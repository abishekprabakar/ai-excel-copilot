"""`xl`: the only helper library compiled programs may call (besides pandas methods).

This file is shipped as *source text* into the sandbox process and executed
there, so it must only import pandas and numpy.
"""
import numpy as np
import pandas as pd

MAX_ROWS = 2_000_000


def mask(cond):
    """Boolean mask with missing values treated as False (Excel-like filtering)."""
    if isinstance(cond, (bool, np.bool_)):
        return cond
    return cond.fillna(False).astype(bool)


def div(a, b):
    """Division where x/0 is missing (Excel shows #DIV/0!), never +/-inf."""
    with np.errstate(divide="ignore", invalid="ignore"):
        out = a / b
    if isinstance(out, (pd.Series, pd.DataFrame)):
        return out.replace([np.inf, -np.inf], np.nan)
    return np.nan if out in (np.inf, -np.inf) else out


def days_between(start, end):
    return (end - start).dt.days


def abs_(x):
    return x.abs() if hasattr(x, "abs") else abs(x)


def round_(x, n):
    return x.round(n) if hasattr(x, "round") else round(x, n)


def isnull(x):
    return x.isna() if hasattr(x, "isna") else pd.isna(x)


def coalesce(a, b):
    return a.fillna(b) if hasattr(a, "fillna") else (b if pd.isna(a) else a)


def if_else(df, cond, a, b):
    cond = mask(cond)
    idx = df.index
    a = a if isinstance(a, pd.Series) else pd.Series(a, index=idx)
    b = b if isinstance(b, pd.Series) else pd.Series(b, index=idx)
    return a.where(cond, b)


def with_column(df, name, value):
    out = df.copy()
    out[name] = value
    return out


_AGG = {"sum": "sum", "mean": "mean", "median": "median", "min": "min", "max": "max",
        "count": "count", "nunique": "nunique", "std": "std"}


def aggregate(df, by, aggs):
    """aggs: [(column, func, as_name)]; column '*' with count = number of rows."""
    work = df.copy()
    spec = {}
    for col, func, name in aggs:
        if col == "*":
            work["__rows__"] = 1
            spec[name] = ("__rows__", "sum")
        else:
            spec[name] = (col, _AGG[func])
    if by:
        out = work.groupby(list(by), dropna=False, sort=True).agg(**spec).reset_index()
    else:
        out = work.assign(__all__=0).groupby("__all__").agg(**spec).reset_index(drop=True)
    for col, func, name in aggs:
        if func == "sum" and col != "*":
            # a sum over only-missing values is missing, not 0
            counts = (work.groupby(list(by), dropna=False, sort=True)[col].count().reset_index(drop=True)
                      if by else pd.Series([work[col].count()]))
            out.loc[counts.values == 0, name] = np.nan
    return out


def join(left, right, left_on, right_on, how):
    same = {r for l, r in zip(left_on, right_on) if l == r}
    ren = {c: f"{c}_right" for c in right.columns if c in left.columns and c not in same}
    r2 = right.rename(columns=ren)
    ro = [ren.get(c, c) for c in right_on]
    # estimate the result size before materialising it (many-to-many explosions)
    lk = left[left_on].astype(str).agg("|".join, axis=1).value_counts()
    rk = r2[ro].astype(str).agg("|".join, axis=1).value_counts()
    est = int((lk * rk.reindex(lk.index).fillna(0)).sum()) + (len(left) if how == "left" else 0)
    if est > MAX_ROWS:
        raise ValueError(f"join would produce about {est:,} rows (limit {MAX_ROWS:,}); "
                         "the keys are not unique on either side")
    return left.merge(r2, left_on=list(left_on), right_on=ro, how=how)


def pivot(df, index, columns, values, func):
    out = df.pivot_table(index=list(index), columns=columns, values=values, aggfunc=_AGG[func],
                         dropna=False, observed=True)
    out.columns = [str(c) for c in out.columns]
    return out.reset_index()
