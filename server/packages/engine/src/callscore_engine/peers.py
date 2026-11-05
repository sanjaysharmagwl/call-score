"""Point-in-time peer-group assignment (decision 0003), shared by metrics and composites."""

from datetime import date

import polars as pl

from callscore_core.enums import PeerDimension

ALL_PEERS = "all"
_COLUMNS = {
    PeerDimension.SECTOR.value: "sector_id",
    PeerDimension.REGION.value: "region",
    PeerDimension.CAP_BUCKET.value: "cap_bucket",
}


def assign_peer_groups(
    coverage_spells: pl.DataFrame,
    classifications: pl.DataFrame,
    as_of_date: date,
    dims: tuple[PeerDimension, ...],
) -> pl.DataFrame:
    """``analyst_id, peer_group`` for every analyst with coverage on or before ``as_of_date``.

    For each dimension the analyst takes the value with the most covered calendar days,
    counted over coverage spells active on ``as_of_date`` (or, if none are active, over all
    spells up to it), intersected with the security's classification spells. Ties break to
    the smallest value. Spell ``to_date`` is inclusive; null is open-ended. The key is
    ``"dim=value|..."`` over sorted dims, or ``"all"`` when ``dims`` is empty.
    """
    spells = coverage_spells.filter(pl.col("from_date") <= as_of_date).with_columns(
        pl.min_horizontal(pl.col("to_date").fill_null(as_of_date), pl.lit(as_of_date)).alias("end"),
        (pl.col("to_date").is_null() | (pl.col("to_date") >= as_of_date)).alias("active"),
    )
    spells = spells.filter(pl.col("active") | ~pl.col("active").any().over("analyst_id")).select(
        "analyst_id", "security_id", pl.col("from_date").alias("start"), "end"
    )
    analysts = spells.select("analyst_id").unique().sort("analyst_id")
    if not dims:
        return analysts.with_columns(pl.lit(ALL_PEERS).alias("peer_group"))

    names = sorted(d.value for d in dims)
    overlap = (
        spells.join(classifications, on="security_id", how="inner")
        .with_columns(
            pl.max_horizontal("start", "from_date").alias("o_start"),
            pl.min_horizontal("end", pl.col("to_date").fill_null(as_of_date)).alias("o_end"),
        )
        .with_columns(((pl.col("o_end") - pl.col("o_start")).dt.total_days() + 1).alias("days"))
        .filter(pl.col("days") > 0)
    )
    result = analysts
    for name in names:
        column = _COLUMNS[name]
        best = (
            overlap.group_by("analyst_id", column)
            .agg(pl.col("days").sum())
            .sort(["analyst_id", "days", column], descending=[False, True, False])
            .unique(subset="analyst_id", keep="first", maintain_order=True)
            .select("analyst_id", pl.col(column).cast(pl.String).alias(name))
        )
        result = result.join(best, on="analyst_id", how="left")
    return result.select(
        "analyst_id",
        pl.when(pl.all_horizontal(pl.col(n).is_not_null() for n in names))
        .then(pl.concat_str([pl.lit(f"{n}=") + pl.col(n) for n in names], separator="|"))
        .otherwise(None)
        .alias("peer_group"),
    )
