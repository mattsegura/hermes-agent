"""P3 cross-business priors + the "what Hermes learned" read-model.

Covers (per the P3 spec):

* Pure shrinkage / pooled-prior math (context matching, shrinkage decay,
  Beta/Normal pooled priors) with no DB.
* Storage topology is one DB per board; pooling reads across sibling board
  DBs read-only. A brand-new board with zero local data warm-starts toward the
  pooled cross-business posterior (its first proposal reflects the better arm
  learned elsewhere).
* Shrinkage: as local outcomes accumulate favoring a different arm, local data
  overrides the pooled prior.
* Pooling is by ``context_features`` similarity -- a board in an unrelated
  domain does NOT borrow.
* The cross-board read is best-effort: a missing / unreadable sibling board
  never breaks a board's own optimizer tick.
* The learned-state read-model returns the expected fields, and the dashboard
  plugin surfaces it.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_optimizer as opt


KNOB = "follow_up_interval_hours"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _contract_with_knob(spec: dict) -> dict:
    return {
        "objective": {
            "statement": "Optimize seller follow-up cadence",
            "success": ["sellers convert"],
            "failure": ["loops run forever"],
            "constraints": ["no real outbound"],
        },
        "runtime": {"mode": "company", "dispatcher": {"profile": "mock-ceo"}},
        "workflow": {
            "id": "opt-flow",
            "stages": [{"key": "execute", "actions": [{"key": "nudge"}]}],
        },
        "tunables": {KNOB: spec},
    }


def _make_board(slug: str, spec: dict, *, context: dict | None = None) -> None:
    kb.create_board(slug)
    kb.write_board_metadata(
        slug, business_contract=_contract_with_knob(spec), context=context,
    )


def _emit_outcome(conn, *, board, knob, value, reward_kind, reward_value, ts, context):
    with kb.write_txn(conn):
        kb.record_board_signal(
            conn,
            board=board,
            primitive_kind="outcome",
            primitive_key="seller_follow_up",
            knob_snapshot={knob: value},
            context_features=context,
            reward_value=reward_value,
            reward_kind=reward_kind,
            realized_at=ts,
            ts=ts,
        )


def _seed_sibling(slug, *, context, good_arm, bad_arm, n=20):
    """Create a sibling board whose history makes ``good_arm`` clearly win."""
    _make_board(slug, {"default": bad_arm, "allowed": [good_arm, bad_arm]}, context=context)
    base = 1_000_000
    with kb.connect(board=slug) as conn:
        for i in range(n):
            _emit_outcome(
                conn, board=slug, knob=KNOB, value=good_arm,
                reward_kind="conversion", reward_value=1.0, ts=base + i, context=context,
            )
            _emit_outcome(
                conn, board=slug, knob=KNOB, value=bad_arm,
                reward_kind="conversion", reward_value=0.0, ts=base + i, context=context,
            )


def _majority_proposal(conn, *, board, knob, draws=40):
    counts: dict = {}
    for seed in range(draws):
        p = opt.propose_knob_value(conn, board=board, knob=knob, rng=random.Random(seed))
        counts[p.proposed_value] = counts.get(p.proposed_value, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 1. Pure math: context matching + shrinkage + pooled priors (no DB)
# ---------------------------------------------------------------------------


def test_context_matches_is_coarse_but_conservative():
    re1 = {"domain": "real_estate", "segment": "wholesaling"}
    re2 = {"domain": "real_estate"}  # only domain shared
    other = {"domain": "dog_grooming"}
    conflict = {"domain": "real_estate", "segment": "flipping"}

    assert opt.context_matches(re1, re2) is True            # same domain pools
    assert opt.context_matches(re1, other) is False         # unrelated domain
    assert opt.context_matches(re1, conflict) is False      # shared key disagrees
    assert opt.context_matches(re1, {}) is False            # no shared key
    assert opt.context_matches({}, re1) is False


def test_shrinkage_weight_decays_with_local_evidence():
    k = opt.CROSS_PRIOR_STRENGTH
    assert opt.shrinkage_weight(0) == pytest.approx(1.0)        # cold start -> prior governs
    assert opt.shrinkage_weight(k) == pytest.approx(0.5)        # halves at local_n == kappa
    # Strictly decreasing toward 0 as local evidence grows.
    assert opt.shrinkage_weight(0) > opt.shrinkage_weight(5) > opt.shrinkage_weight(50)
    assert opt.shrinkage_weight(10_000) < 0.01


def test_build_pooled_priors_binary_encodes_sibling_rates():
    pooled = {
        opt._arm_key(48): opt.ArmEvidence(value=48, family="binary", n=30, success=27.0),
        opt._arm_key(72): opt.ArmEvidence(value=72, family="binary", n=30, success=3.0),
    }
    weight, priors, total = opt.build_pooled_priors("binary", pooled, [48, 72], local_n=0)
    assert weight == pytest.approx(1.0)
    # The better sibling arm has a much higher prior mean.
    p48 = priors[opt._arm_key(48)]
    p72 = priors[opt._arm_key(72)]
    assert p48.alpha / (p48.alpha + p48.beta) > 0.8
    assert p72.alpha / (p72.alpha + p72.beta) < 0.2
    # Pseudo-count mass is capped per arm and summed for the floor credit.
    assert p48.pseudo_count == pytest.approx(opt.MAX_POOLED_PSEUDO)
    assert total == pytest.approx(2 * opt.MAX_POOLED_PSEUDO)


def test_build_pooled_priors_collapses_as_local_grows():
    pooled = {opt._arm_key(48): opt.ArmEvidence(value=48, family="binary", n=30, success=30.0)}
    _, priors_cold, _ = opt.build_pooled_priors("binary", pooled, [48, 72], local_n=0)
    _, priors_mature, total_mature = opt.build_pooled_priors(
        "binary", pooled, [48, 72], local_n=1000,
    )
    cold = priors_cold[opt._arm_key(48)].pseudo_count
    mature = priors_mature[opt._arm_key(48)].pseudo_count
    assert cold > mature
    assert mature < 0.5  # a mature board barely feels the prior


# ---------------------------------------------------------------------------
# 2. Storage topology: one DB per board (documented assumption made explicit)
# ---------------------------------------------------------------------------


def test_storage_topology_is_one_db_per_board(fresh_home):
    _make_board("alpha", {"default": 72, "allowed": [48, 72]}, context={"domain": "x"})
    _make_board("beta", {"default": 72, "allowed": [48, 72]}, context={"domain": "x"})
    pa = kb.kanban_db_path(board="alpha").resolve()
    pb = kb.kanban_db_path(board="beta").resolve()
    pdefault = kb.kanban_db_path(board="default").resolve()
    # Three distinct DB files -> pooling must read across siblings.
    assert len({pa, pb, pdefault}) == 3


# ---------------------------------------------------------------------------
# 3. Warm start: zero local data borrows the better arm from similar boards
# ---------------------------------------------------------------------------


def test_new_board_warm_starts_from_similar_businesses(fresh_home):
    ctx = {"domain": "real_estate"}
    _seed_sibling("sib-a", context=ctx, good_arm=48, bad_arm=72)
    _seed_sibling("sib-b", context=ctx, good_arm=48, bad_arm=72)

    # Brand-new board, SAME domain, ZERO local outcomes, currently parked at 72.
    _make_board("newco", {"default": 72, "allowed": [48, 72]}, context=ctx)
    with kb.connect(board="newco") as conn:
        # Sanity: pooling actually found the siblings' outcomes.
        fam, pooled, meta = kb.read_cross_business_evidence(
            target_board="newco", knob=KNOB,
            spec={"default": 72, "allowed": [48, 72]},
        )
        assert fam == "binary"
        assert meta["pooled_outcomes"] == 80
        assert {m["board"] for m in meta["matched_boards"]} == {"sib-a", "sib-b"}

        counts = _majority_proposal(conn, board="newco", knob=KNOB)
        # With no local data the warm start points at the arm learned elsewhere.
        assert counts.get(48, 0) >= 0.9 * sum(counts.values())


# ---------------------------------------------------------------------------
# 4. Shrinkage: enough local evidence overrides the pooled prior
# ---------------------------------------------------------------------------


def test_local_evidence_overrides_pooled_prior(fresh_home):
    ctx = {"domain": "real_estate"}
    # Siblings strongly prefer arm 48.
    _seed_sibling("sib-a", context=ctx, good_arm=48, bad_arm=72)
    _seed_sibling("sib-b", context=ctx, good_arm=48, bad_arm=72)

    # This board's OWN data strongly prefers arm 72 instead. It is currently
    # parked at 48 (the pooled-preferred arm).
    _make_board("mature", {"default": 48, "allowed": [48, 72]}, context=ctx)
    base = 5_000_000
    with kb.connect(board="mature") as conn:
        for i in range(30):
            _emit_outcome(
                conn, board="mature", knob=KNOB, value=72,
                reward_kind="conversion", reward_value=1.0, ts=base + i, context=ctx,
            )
        for i in range(8):
            _emit_outcome(
                conn, board="mature", knob=KNOB, value=48,
                reward_kind="conversion", reward_value=0.0, ts=base + 100 + i, context=ctx,
            )
        counts = _majority_proposal(conn, board="mature", knob=KNOB)
        # Local data wins: the proposal flips to 72 despite the pooled 48-prior.
        assert counts.get(72, 0) >= 0.9 * sum(counts.values())


# ---------------------------------------------------------------------------
# 5. Pooling is by context similarity: unrelated domain does NOT borrow
# ---------------------------------------------------------------------------


def test_unrelated_domain_does_not_borrow(fresh_home):
    # A well-learned board in a DIFFERENT domain.
    _seed_sibling("groomer", context={"domain": "dog_grooming"}, good_arm=48, bad_arm=72)

    # New board in real_estate with no similar siblings and no local data.
    _make_board("newco", {"default": 72, "allowed": [48, 72]}, context={"domain": "real_estate"})
    with kb.connect(board="newco") as conn:
        fam, pooled, meta = kb.read_cross_business_evidence(
            target_board="newco", knob=KNOB,
            spec={"default": 72, "allowed": [48, 72]},
        )
        assert meta["pooled_outcomes"] == 0
        assert meta["matched_boards"] == []
        assert not pooled

        # No pooled prior + no local data -> the knob is left exactly where it is.
        proposal = opt.propose_knob_value(
            conn, board="newco", knob=KNOB, rng=random.Random(0),
        )
        assert proposal.changed is False
        assert proposal.proposed_value == 72
        assert proposal.reason == "insufficient_evidence"


# ---------------------------------------------------------------------------
# 6. Best-effort cross-board read: a broken sibling never breaks the tick
# ---------------------------------------------------------------------------


def test_unreadable_sibling_board_does_not_break_pooling(fresh_home):
    ctx = {"domain": "real_estate"}
    _seed_sibling("sib-good", context=ctx, good_arm=48, bad_arm=72)
    _seed_sibling("sib-bad", context=ctx, good_arm=48, bad_arm=72)

    # Corrupt the "bad" sibling's DB file so any read raises -> must be swallowed.
    bad_path = kb.kanban_db_path(board="sib-bad")
    bad_path.write_bytes(b"this is not a sqlite database at all")

    _make_board("newco", {"default": 72, "allowed": [48, 72]}, context=ctx)
    with kb.connect(board="newco") as conn:
        fam, pooled, meta = kb.read_cross_business_evidence(
            target_board="newco", knob=KNOB,
            spec={"default": 72, "allowed": [48, 72]},
        )
        # The good sibling still contributes; the broken one is silently skipped.
        assert {m["board"] for m in meta["matched_boards"]} == {"sib-good"}
        assert meta["pooled_outcomes"] == 40

        # And the optimizer tick runs end-to-end without raising.
        res = kb.optimizer_tick(conn, board="newco", now=9_000_000, rng=random.Random(0))
        assert "applied" in res


def test_missing_sibling_path_reads_empty(fresh_home):
    obs = kb._read_sibling_outcome_observations(
        fresh_home / "does" / "not" / "exist.db", KNOB,
    )
    assert obs == []


# ---------------------------------------------------------------------------
# 7. Signal enrichment: declared board context is stamped onto signals
# ---------------------------------------------------------------------------


def test_signal_context_features_overlays_declared_context(fresh_home):
    _make_board(
        "declared", {"default": 72, "allowed": [48, 72]},
        context={"domain": "real_estate", "segment": "wholesaling"},
    )
    feats = kb._signal_context_features("declared", None)
    assert feats["domain"] == "real_estate"      # declared domain overrides slug
    assert feats["segment"] == "wholesaling"


# ---------------------------------------------------------------------------
# 8. Learned-state read-model + dashboard surface
# ---------------------------------------------------------------------------


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_xbiz_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


def test_learned_state_read_model_fields(fresh_home):
    ctx = {"domain": "real_estate"}
    _seed_sibling("sib-a", context=ctx, good_arm=48, bad_arm=72)

    _make_board("liveco", {"default": 72, "allowed": [48, 72]}, context=ctx)
    base = 6_000_000
    with kb.connect(board="liveco") as conn:
        # Some local outcomes...
        for i in range(6):
            _emit_outcome(
                conn, board="liveco", knob=KNOB, value=48,
                reward_kind="conversion", reward_value=1.0, ts=base + i, context=ctx,
            )
        # ...and a recorded autonomous knob change for the "last change" field.
        kb.apply_knob_update(conn, board="liveco", knob=KNOB, new_value=48, reason="test")

        model = kb.build_learned_state_read_model(conn, board="liveco")
        assert model["board"] == "liveco"
        assert len(model["knobs"]) == 1
        knob = model["knobs"][0]

        assert knob["knob"] == KNOB
        assert knob["current_value"] == 48          # the applied change moved it
        assert knob["allowed"] == [48, 72]
        assert knob["reward_family"] == "binary"
        assert knob["min_evidence"] == opt.MIN_EVIDENCE_OUTCOMES

        # Per-arm posterior mean +/- uncertainty from the same learner.
        arms = {a["value"]: a for a in knob["arms"]}
        assert set(arms) == {48, 72}
        for a in arms.values():
            assert "posterior_mean" in a and "posterior_stddev" in a
            assert a["posterior_stddev"] >= 0.0
            assert "reward_trend" in a and "history" in a["reward_trend"]
        assert arms[48]["local_outcomes"] == 6
        assert arms[48]["pooled_outcomes"] == 20

        # Last change: autonomous (optimizer applied it in-bounds).
        lc = knob["last_change"]
        assert lc is not None
        assert lc["new_value"] == "48"
        assert lc["mode"] == "autonomous"

        # Cross-business influence is surfaced with its shrinkage weight.
        xb = knob["cross_business"]
        assert xb["influencing"] is True
        assert 0.0 < xb["shrinkage_weight"] <= 1.0
        assert xb["pooled_outcomes"] == 40
        assert {m["board"] for m in xb["matched_boards"]} == {"sib-a"}


def test_learned_state_dashboard_endpoint(fresh_home, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(Path, "home", lambda: fresh_home.parent)
    ctx = {"domain": "real_estate"}
    _seed_sibling("sib-a", context=ctx, good_arm=48, bad_arm=72)
    _make_board("dashco", {"default": 72, "allowed": [48, 72]}, context=ctx)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    client = TestClient(app)

    resp = client.get("/api/plugins/kanban/learned-state", params={"board": "dashco"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["board"] == "dashco"
    assert len(body["knobs"]) == 1
    knob = body["knobs"][0]
    assert knob["knob"] == KNOB
    assert knob["cross_business"]["pooled_outcomes"] == 40
    assert knob["arms"]
