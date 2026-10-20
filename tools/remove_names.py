#!/usr/bin/env python3
"""Remove a list of names from a built dictionary parquet, without rebuilding it.

Rewrites a new dictionary from the one that already shipped. Needs ``pip install duckdb``.

Matching ignores case and extra whitespace, and routes by entry shape: "Taylor Swift"
drops rows with that full_name, "Swift" drops rows with that first_name or last_name.
(``remove_names(match=...)`` can force one column; the CLI does not expose it.)

Rewriting the shipped 1.2B rows takes ~5 minutes and spills ~60 GB (``kept_rows_sql``
says why it sorts). It also repacks row groups, which costs the boundary alignment that
kept gender runs apart (638 of 9,659 mixed becomes 2,153 of 9,798), and the holes left
behind compress worse (4.04 -> 4.13 GB) against a file already at the LFS size limit.

Usage:
    python tools/remove_names.py blocked.txt              # preview, writes nothing
    python tools/remove_names.py blocked.txt --in-place    # clean the shipped dictionary
    python tools/remove_names.py blocked.txt --out clean.parquet
"""

import argparse
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from build_name_dictionary import load_blocklist, load_surnames, verify

DATA = Path(__file__).resolve().parent.parent / "limina_postprocessor" / "data"
DEFAULT_DICTIONARY = DATA / "name_dictionary_1b_filtered.parquet"
CENSUS_DIR = DATA / "census_data"

# "component" bans a name in either position.
MATCH_COLUMNS = {
    "full": ("full_name",),
    "first": ("first_name",),
    "last": ("last_name",),
    "component": ("first_name", "last_name"),
}
MATCH_MODES = ("auto",) + tuple(MATCH_COLUMNS)

# Skipped as a header on line 1 only; famous_names_blocklist.txt has one.
HEADERS = {"NAME", "NAMES", "FULL_NAME", "FIRST_NAME", "LAST_NAME", "SURNAME"}


# --------------------------------------------------------------------------- the list


def names_array(values) -> pa.ChunkedArray:
    """Upper-case, trim, collapse inner whitespace, drop blanks.

    Arrow's version of ``build_name_dictionary.normalize`` so a list need not be written
    in the dictionary's casing.
    """
    array = pa.array([str(value) for value in values], type=pa.string())
    array = pc.replace_substring_regex(pc.utf8_upper(pc.utf8_trim_whitespace(array)), r"\s+", " ")
    return pa.chunked_array([array.filter(pc.greater(pc.utf8_length(array), 0))], type=pa.string())


def load_names(path: Path) -> pa.ChunkedArray:
    """Read the list: one name per line, or a JSON list / {name: ...} mapping."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        if handle.read(64).lstrip().startswith(("[", "{")):
            return names_array(sorted(load_blocklist(path)))

        handle.seek(0)
        names = []
        for index, line in enumerate(handle):
            line = line.strip()
            if line and not line.startswith("#") and not (index == 0 and line.upper() in HEADERS):
                names.append(line)
    return names_array(names)


# ----------------------------------------------------------------------------- duckdb


def escape(value) -> str:
    """Escape a value for use inside a SQL string literal."""
    return str(value).replace("'", "''")


def count_of(connection, table: str) -> int:
    """Rows in a key table."""
    return connection.sql(f"SELECT count(*) FROM {table}").fetchone()[0]


def register_keys(connection, names: pa.ChunkedArray, match: str) -> dict:
    """Load the list into DuckDB. Returns {dictionary column: key table}.

    ``auto`` splits by token count; other modes send the whole list to their columns.
    """
    connection.register("names_raw", pa.table({"key": names}))
    targets = {}

    if match == "auto":
        connection.execute("CREATE TABLE full_keys AS SELECT DISTINCT key FROM names_raw WHERE position(' ' IN key) > 0")
        connection.execute("CREATE TABLE token_keys AS SELECT DISTINCT key FROM names_raw WHERE position(' ' IN key) = 0")
        if count_of(connection, "full_keys"):
            targets["full_name"] = "full_keys"
        if count_of(connection, "token_keys"):
            targets["first_name"] = "token_keys"
            targets["last_name"] = "token_keys"
    else:
        connection.execute("CREATE TABLE keys AS SELECT DISTINCT key FROM names_raw")
        if count_of(connection, "keys"):
            targets = {column: "keys" for column in MATCH_COLUMNS[match]}
    return targets


def kept_rows_sql(source, columns: list, targets: dict) -> str:
    """SELECT the surviving rows in the source's row order.

    One ANTI JOIN per target column; chained, they mean "matches no key anywhere".

    Keep the ORDER BY. Row order *is* the layout (surname runs keep ``last_name``
    dictionary-encoded, gender runs let ``_index_row_groups`` route by statistics), and
    every cheaper arrangement reordered rows: plain anti join, single-threaded COPY
    (ordered for 740k of 1.2B rows, then not), keys inlined as NOT IN (ordered, but
    unfinished after 7min at 10k keys). ``--verify`` misses this; only the tests catch it.
    """
    projection = ", ".join(f"d.{column}" for column in columns)
    joins = "".join(f" ANTI JOIN {table} AS k{index} ON upper(d.{column}) = k{index}.key" for index, (column, table) in enumerate(targets.items()))
    return f"SELECT {projection} FROM read_parquet('{escape(source)}', file_row_number=true) AS d{joins} ORDER BY d.file_row_number"


def measure(connection, source, targets: dict) -> dict:
    """One scan: rows and distinct names the list hits, per column.

    LEFT JOIN, not the write's ANTI JOIN, so one pass counts both. Keys are DISTINCT, so
    no join duplicates a row.
    """
    selects = ["count(*) AS rows", "count(DISTINCT d.last_name) AS surnames"]
    joins, hits = "", []
    for index, (column, table) in enumerate(targets.items()):
        joins += f" LEFT JOIN {table} AS k{index} ON upper(d.{column}) = k{index}.key"
        selects.append(f"count(*) FILTER (WHERE k{index}.key IS NOT NULL) AS rows_{column}")
        selects.append(f"count(DISTINCT k{index}.key) AS names_{column}")
        hits.append(f"k{index}.key")
    selects.append(f"count(*) FILTER (WHERE coalesce({', '.join(hits)}) IS NOT NULL) AS removed")

    row = connection.sql(f"SELECT {', '.join(selects)} FROM read_parquet('{escape(source)}') AS d{joins}")
    return dict(zip(row.columns, row.fetchone()))


def remove_names(source, out, names, match="auto", row_group_size=None, threads=None, temp_dir=None) -> dict:
    """Rewrite ``source`` to ``out`` without the rows matching ``names``.

    ``names`` comes from ``load_names``; ``match`` is one of ``MATCH_MODES``. Returns
    stats (key counts, hits, rows in/out); ``out=None`` reports hits without writing.
    Hits are always measured -- 11s against a 288s rewrite, and the only sign that a list
    matched anything. Pass ``temp_dir``: DuckDB spills the sort to ``.tmp`` under the
    *current* directory otherwise, filling a repo checkout.
    """
    import duckdb

    metadata = pq.ParquetFile(source, memory_map=True).metadata
    if not metadata.num_rows:
        raise ValueError(f"{source} is empty")
    columns = metadata.schema.names
    if row_group_size is None:
        # The handler's sampling budget assumes the source's row group size.
        row_group_size = metadata.row_group(0).num_rows

    connection = duckdb.connect()
    try:
        connection.execute("SET preserve_insertion_order = true")
        if threads:
            connection.execute(f"SET threads = {int(threads)}")
        if temp_dir is not None:
            connection.execute(f"SET temp_directory = '{escape(temp_dir)}'")

        targets = register_keys(connection, names, match)
        if not targets:
            raise ValueError("removal list is empty after normalization, nothing to remove")
        missing = [column for column in targets if column not in columns]
        if missing:
            raise ValueError(f"{source} has no {missing} column(s); it holds {columns}")

        stats = {
            "rows_in": metadata.num_rows,
            "keys": {table: count_of(connection, table) for table in dict.fromkeys(targets.values())},
            "targets": dict(targets),
        }

        started = time.perf_counter()
        stats["measured"] = measure(connection, source, targets)
        stats["measure_seconds"] = time.perf_counter() - started

        if out is not None:
            started = time.perf_counter()
            connection.execute(
                f"COPY ({kept_rows_sql(source, columns, targets)}) TO '{escape(out)}' " f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {row_group_size})"
            )
            stats["write_seconds"] = time.perf_counter() - started
            stats["rows_out"] = pq.ParquetFile(out, memory_map=True).metadata.num_rows
            stats["rows_removed"] = stats["rows_in"] - stats["rows_out"]
        return stats
    finally:
        connection.close()


def gender_groups(path) -> tuple:
    """(row groups, groups whose gender statistic is not one value), from metadata.

    The second number is how many row groups ``name_handler`` must filter after reading,
    so it is the cheapest signal on whether the gender runs survived.
    """
    metadata = pq.ParquetFile(path, memory_map=True).metadata
    if "gender" not in metadata.schema.names:
        return metadata.num_row_groups, None

    index = metadata.schema.names.index("gender")
    mixed = 0
    for row_group in range(metadata.num_row_groups):
        stats = metadata.row_group(row_group).column(index).statistics
        if stats is None or stats.min != stats.max:
            mixed += 1
    return metadata.num_row_groups, mixed


# -------------------------------------------------------------------------------- cli


def report(source, target, stats) -> None:
    """Print what the list hit and, if anything was written, what it cost."""
    rows_in, measured = stats["rows_in"], stats["measured"]
    for table, count in stats["keys"].items():
        print(f"  {count:>12,} distinct -> {', '.join(c for c, t in stats['targets'].items() if t == table)}")
    print(f"dictionary           : {source} ({rows_in:,} rows)")
    for column in stats["targets"]:
        rows = measured[f"rows_{column}"]
        print(f"  {column:11s} matches {measured[f'names_{column}']:,} names, {rows:,} rows ({rows / rows_in:.3%})")
    label = "would remove" if target is None else "to remove"
    print(f"{label:21s}: {measured['removed']:,} rows ({measured['removed'] / rows_in:.3%}) in {stats['measure_seconds']:.1f}s")
    if target is None:
        return

    size_in, size_out = source.stat().st_size, target.stat().st_size
    groups_out, mixed_out = gender_groups(target)
    groups_in, mixed_in = gender_groups(source)
    print(f"wrote                : {target} in {stats['write_seconds']:.1f}s")
    print(f"rows                 : {stats['rows_out']:,} kept, {stats['rows_removed']:,} removed")
    # Holes hurt last_name's compression, so the output can grow while holding less.
    print(
        f"file size            : {size_out / 1e9:.2f} GB ({size_out / max(stats['rows_out'], 1):.2f} bytes/row), "
        f"was {size_in / 1e9:.2f} GB ({size_in / rows_in:.2f} bytes/row)"
    )
    layout = f"row groups           : {groups_out:,} (was {groups_in:,})"
    print(layout + (f", {mixed_out:,} mixed-gender (was {mixed_in:,})" if mixed_out is not None else ""))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("blocklist", type=Path, help="names to remove (one per line, or JSON)")
    parser.add_argument("--dictionary", type=Path, default=DEFAULT_DICTIONARY, help="parquet to clean (default: the shipped dictionary)")
    parser.add_argument("--in-place", action="store_true", help="replace the dictionary (default: preview only)")
    parser.add_argument("--temp-dir", type=Path, help="where the sort spills, ~60 GB (default: beside the dictionary)")
    args = parser.parse_args(argv)

    source = args.dictionary
    # Write a sibling file and rename it, so readers see one file or the other. The temp
    # file is ours, so a leftover from an earlier run is overwritten rather than refused.
    target = source.with_suffix(".cleaning.parquet") if args.in_place else None

    # Spill beside the dictionary, not into ``.tmp`` under the current directory.
    temp_dir = args.temp_dir or (source.parent if target is not None else None)

    names = load_names(args.blocklist)
    # Flushed: the scan and write that follow run for minutes without output.
    print(f"removal list         : {len(names):,} names from {args.blocklist}", flush=True)
    if temp_dir is not None:
        print(f"sort scratch         : {temp_dir} (~60 GB for a 1.2B-row dictionary)", flush=True)

    try:
        stats = remove_names(source, target, names, temp_dir=temp_dir)
    except ValueError as error:
        parser.error(str(error))

    report(source, target, stats)

    if target is None:
        print("preview only         : pass --in-place to write the result")
        return 0

    # Surnames the list removed on purpose are not expected to survive; verify() catches
    # any others that vanished. Always run: the swap below cannot be undone.
    measured = stats["measured"]
    expected = measured["surnames"] - measured.get("names_last_name", 0)
    shipped = len(load_surnames(CENSUS_DIR))
    if expected != shipped:
        print(f"expected surnames    : {expected:,} (census ships {shipped:,})")
    if not verify(target, expected):
        print(f"\nNOT replacing {source}; the rewrite is at {target}")
        return 1

    target.replace(source)  # atomic within a filesystem
    print(f"replaced             : {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
