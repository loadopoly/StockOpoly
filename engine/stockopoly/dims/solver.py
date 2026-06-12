"""Relational dimension solver — pure math, no DB, no third-party imports.

Every dimension is an unknown in **log space** (x = ln inches), as is every
photo's scale (s = ln inches-per-pixel). Measurement kinds become residuals:

    absolute            x_a = ln(value)
    distance_estimate   x_a = ln(value)            (high sigma; EXIF-derived)
    pixel_extent        x_a − s_photo = ln(pixels)
    ratio               x_a − x_b = ln(value)
    identity            x_a − x_b = 0
    pattern_count       x_a − x_b = ln(count)
    sum_parts           exp(x_a) − Σ exp(x_part) = 0   (nonlinear, abs space)

All but ``sum_parts`` are linear in x, so Gauss–Newton converges in one step
without sums and in a few steps with them. Robustness comes from IRLS with a
Huber weight on whitened residuals; gross outliers are flagged back to the
caller. Components of the measurement graph that contain no absolute anchor
are *ungrounded*: their shape is solved under a unit gauge but they are
reported so the UI can ask for a tape-measure / reference shot.

numpy, when installed, accelerates the dense solve; the pure-Python
Gaussian-elimination fallback handles the small systems (≤ a few hundred
unknowns) this app produces.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

try:
    import numpy as _np  # type: ignore
    _LINALG_ERRORS: tuple = (ArithmeticError, _np.linalg.LinAlgError)
except ImportError:  # pragma: no cover - environment dependent
    _np = None
    _LINALG_ERRORS = (ArithmeticError,)

HUBER_K = 2.5
OUTLIER_Z = 4.0
_GAUGE_SIGMA = 10.0     # weak prior pinning one var of an ungrounded component
_RIDGE = 1e-9

LINEAR_KINDS = {"absolute", "distance_estimate", "pixel_extent", "ratio",
                "identity", "pattern_count"}
ANCHOR_KINDS = {"absolute", "distance_estimate"}


@dataclass
class Measurement:
    kind: str
    a: str                          # variable key
    value: float                    # inches / pixels / ratio / count
    b: str | None = None            # second variable (ratio/identity/pattern)
    photo: str | None = None        # photo-scale key (pixel_extent)
    parts: list[str] = field(default_factory=list)  # sum_parts members
    sigma_ln: float = 0.05          # log-space sigma (linear kinds)
    sigma_abs: float | None = None  # absolute-space sigma (sum_parts)
    meas_id: Any = None


@dataclass
class SolveResult:
    values: dict[str, float]        # var key → inches
    sigmas_ln: dict[str, float]
    scales: dict[str, float]        # photo key → inches per pixel
    scale_sigmas_ln: dict[str, float]
    grounded: dict[str, bool]       # var/photo key → anchored?
    components: list[dict[str, Any]]
    outliers: list[Any]             # meas_ids with |z| > OUTLIER_Z
    iterations: int
    rms: float


class _UF:
    def __init__(self):
        self.p: dict[str, str] = {}

    def add(self, k: str) -> None:
        self.p.setdefault(k, k)

    def find(self, k: str) -> str:
        self.add(k)
        while self.p[k] != k:
            self.p[k] = self.p[self.p[k]]
            k = self.p[k]
        return k

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _solve_dense(n: list[list[float]], g: list[float]) -> list[float]:
    """Solve N·x = g. numpy fast path, else partial-pivot Gaussian elimination."""
    if _np is not None:
        try:
            return list(_np.linalg.solve(_np.array(n), _np.array(g)))
        except _np.linalg.LinAlgError as exc:
            raise ArithmeticError(str(exc)) from exc
    size = len(g)
    a = [row[:] + [g[i]] for i, row in enumerate(n)]
    for col in range(size):
        piv = max(range(col, size), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-15:
            raise ArithmeticError("Singular normal matrix")
        a[col], a[piv] = a[piv], a[col]
        inv = 1.0 / a[col][col]
        for r in range(col + 1, size):
            f = a[r][col] * inv
            if f == 0.0:
                continue
            for c in range(col, size + 1):
                a[r][c] -= f * a[col][c]
    x = [0.0] * size
    for r in range(size - 1, -1, -1):
        s = a[r][size] - sum(a[r][c] * x[c] for c in range(r + 1, size))
        x[r] = s / a[r][r]
    return x


def _residual_rows(m: Measurement, x: dict[str, float]
                   ) -> tuple[float, dict[str, float], float] | None:
    """Return (residual, jacobian_row{key: d r/d x}, sigma) for one measurement."""
    if m.kind in ("absolute", "distance_estimate"):
        if m.value <= 0:
            return None
        return x[m.a] - math.log(m.value), {m.a: 1.0}, m.sigma_ln
    if m.kind == "pixel_extent":
        if m.value <= 0 or m.photo is None:
            return None
        return (x[m.a] - x[m.photo] - math.log(m.value),
                {m.a: 1.0, m.photo: -1.0}, m.sigma_ln)
    if m.kind in ("ratio", "pattern_count"):
        if m.value <= 0 or m.b is None:
            return None
        return (x[m.a] - x[m.b] - math.log(m.value),
                {m.a: 1.0, m.b: -1.0}, m.sigma_ln)
    if m.kind == "identity":
        if m.b is None:
            return None
        return x[m.a] - x[m.b], {m.a: 1.0, m.b: -1.0}, m.sigma_ln
    if m.kind == "sum_parts":
        if not m.parts:
            return None
        ea = math.exp(x[m.a])
        parts_sum = sum(math.exp(x[p]) for p in m.parts)
        sigma = m.sigma_abs or max(0.02 * max(ea, parts_sum), 1e-6)
        jac = {m.a: ea}
        for p in m.parts:
            jac[p] = jac.get(p, 0.0) - math.exp(x[p])
        return ea - parts_sum, jac, sigma
    return None


def _seed_initial(keys: list[str], measurements: list[Measurement]) -> dict[str, float]:
    """BFS propagation of anchors through linear relations for a good start."""
    x = {k: 0.0 for k in keys}
    known: set[str] = set()
    for m in measurements:
        if m.kind in ANCHOR_KINDS and m.value > 0:
            x[m.a] = math.log(m.value)
            known.add(m.a)
    for _ in range(len(measurements) + 1):
        progressed = False
        for m in measurements:
            if m.kind in ("ratio", "pattern_count") and m.b and m.value > 0:
                if m.b in known and m.a not in known:
                    x[m.a] = x[m.b] + math.log(m.value); known.add(m.a); progressed = True
                elif m.a in known and m.b not in known:
                    x[m.b] = x[m.a] - math.log(m.value); known.add(m.b); progressed = True
            elif m.kind == "identity" and m.b:
                if m.b in known and m.a not in known:
                    x[m.a] = x[m.b]; known.add(m.a); progressed = True
                elif m.a in known and m.b not in known:
                    x[m.b] = x[m.a]; known.add(m.b); progressed = True
            elif m.kind == "pixel_extent" and m.photo and m.value > 0:
                if m.a in known and m.photo not in known:
                    x[m.photo] = x[m.a] - math.log(m.value); known.add(m.photo); progressed = True
                elif m.photo in known and m.a not in known:
                    x[m.a] = x[m.photo] + math.log(m.value); known.add(m.a); progressed = True
        if not progressed:
            break
    return x


def _components(keys: list[str], measurements: list[Measurement]
                ) -> tuple[dict[str, bool], list[dict[str, Any]]]:
    uf = _UF()
    for k in keys:
        uf.add(k)
    for m in measurements:
        touched = [m.a] + ([m.b] if m.b else []) + ([m.photo] if m.photo else []) + m.parts
        for t in touched[1:]:
            uf.union(touched[0], t)
    anchors: dict[str, int] = {}
    for m in measurements:
        if m.kind in ANCHOR_KINDS:
            root = uf.find(m.a)
            anchors[root] = anchors.get(root, 0) + 1
    groups: dict[str, list[str]] = {}
    for k in keys:
        groups.setdefault(uf.find(k), []).append(k)
    grounded = {}
    comps = []
    for root, members in groups.items():
        ok = anchors.get(root, 0) > 0
        for k in members:
            grounded[k] = ok
        comps.append({"members": sorted(members), "grounded": ok,
                      "anchor_count": anchors.get(root, 0)})
    comps.sort(key=lambda c: (-len(c["members"]), c["members"][0] if c["members"] else ""))
    return grounded, comps


def solve(var_keys: list[str], photo_keys: list[str],
          measurements: list[Measurement], *, max_iter: int = 12,
          huber_passes: int = 2) -> SolveResult:
    keys = list(dict.fromkeys(list(var_keys) + list(photo_keys)))
    usable = [m for m in measurements
              if _residual_rows(m, {k: 0.0 for k in keys}) is not None]
    grounded, comps = _components(keys, usable)
    if not keys:
        return SolveResult({}, {}, {}, {}, {}, comps, [], 0, 0.0)

    index = {k: i for i, k in enumerate(keys)}
    x = _seed_initial(keys, usable)

    # Gauge priors: one weak pin per ungrounded component keeps N invertible.
    gauge: list[str] = [c["members"][0] for c in comps if not c["grounded"] and c["members"]]
    # Keys not touched by any measurement still need a pin.
    touched: set[str] = set()
    for m in usable:
        touched.update([m.a] + ([m.b] if m.b else [])
                       + ([m.photo] if m.photo else []) + m.parts)
    gauge += [k for k in keys if k not in touched and k not in gauge]

    weights = {id(m): 1.0 for m in usable}
    iterations = 0
    rms = 0.0
    for _pass in range(huber_passes + 1):
        for _it in range(max_iter):
            iterations += 1
            n_size = len(keys)
            nmat = [[0.0] * n_size for _ in range(n_size)]
            gvec = [0.0] * n_size
            sq_sum, n_res = 0.0, 0
            for m in usable:
                rr = _residual_rows(m, x)
                if rr is None:
                    continue
                r, jac, sigma = rr
                w = weights[id(m)] / (sigma * sigma)
                sq_sum += (r / sigma) ** 2 * weights[id(m)]
                n_res += 1
                items = list(jac.items())
                for ka, va in items:
                    ia = index[ka]
                    gvec[ia] += w * va * r
                    for kb, vb in items:
                        nmat[ia][index[kb]] += w * va * vb
            for k in gauge:
                i = index[k]
                w = 1.0 / (_GAUGE_SIGMA ** 2)
                nmat[i][i] += w
                gvec[i] += w * x[k]
            for i in range(n_size):
                nmat[i][i] += _RIDGE
            try:
                delta = _solve_dense(nmat, gvec)
            except ArithmeticError:
                break
            for k, i in index.items():
                x[k] -= delta[i]
            rms = math.sqrt(sq_sum / n_res) if n_res else 0.0
            if max((abs(d) for d in delta), default=0.0) < 1e-10:
                break
        if _pass == huber_passes:
            break
        # IRLS Huber reweighting on whitened residuals
        for m in usable:
            rr = _residual_rows(m, x)
            if rr is None:
                continue
            r, _, sigma = rr
            z = abs(r / sigma)
            weights[id(m)] = 1.0 if z <= HUBER_K else HUBER_K / z

    # Covariance diagonal from the last normal matrix (refresh at solution).
    n_size = len(keys)
    nmat = [[0.0] * n_size for _ in range(n_size)]
    for m in usable:
        rr = _residual_rows(m, x)
        if rr is None:
            continue
        _, jac, sigma = rr
        w = weights[id(m)] / (sigma * sigma)
        items = list(jac.items())
        for ka, va in items:
            for kb, vb in items:
                nmat[index[ka]][index[kb]] += w * va * vb
    for k in gauge:
        nmat[index[k]][index[k]] += 1.0 / (_GAUGE_SIGMA ** 2)
    for i in range(n_size):
        nmat[i][i] += _RIDGE
    sig_ln: dict[str, float] = {}
    try:
        if _np is not None:
            cov = _np.linalg.inv(_np.array(nmat))
            for k, i in index.items():
                sig_ln[k] = math.sqrt(max(float(cov[i][i]), 0.0))
        else:
            for k, i in index.items():
                e = [0.0] * n_size
                e[i] = 1.0
                col = _solve_dense(nmat, e)
                sig_ln[k] = math.sqrt(max(col[i], 0.0))
    except _LINALG_ERRORS:
        sig_ln = {k: float("inf") for k in keys}

    outliers = []
    for m in usable:
        rr = _residual_rows(m, x)
        if rr is None:
            continue
        r, _, sigma = rr
        if abs(r / sigma) > OUTLIER_Z and m.meas_id is not None:
            outliers.append(m.meas_id)

    photo_set = set(photo_keys)
    return SolveResult(
        values={k: math.exp(x[k]) for k in keys if k not in photo_set},
        sigmas_ln={k: sig_ln.get(k, float("inf")) for k in keys if k not in photo_set},
        scales={k: math.exp(x[k]) for k in photo_set},
        scale_sigmas_ln={k: sig_ln.get(k, float("inf")) for k in photo_set},
        grounded=grounded,
        components=comps,
        outliers=outliers,
        iterations=iterations,
        rms=round(rms, 6),
    )
