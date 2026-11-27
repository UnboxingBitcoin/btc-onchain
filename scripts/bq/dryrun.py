"""BigQuery DRY RUN only. Reads no data and costs nothing: reports bytes each query WOULD scan."""
import json, os, sys
from google.cloud import bigquery

PROJECT = os.environ["GCP_PROJECT"]
D = "`bigquery-public-data.crypto_bitcoin"
TIB = 1024 ** 4

Q = {
 "backfill_created (outputs: day, value)":
  f"SELECT DATE(block_timestamp) d, SUM(value) v, COUNT(*) n FROM {D}.outputs` GROUP BY d",
 "backfill_spent (inputs x outputs: spend day, created day, value)":
  f"""SELECT DATE(i.block_timestamp) sd, DATE(o.block_timestamp) cd, SUM(i.value) v, COUNT(*) n
      FROM {D}.inputs` i JOIN {D}.outputs` o
      ON i.spent_transaction_hash = o.transaction_hash AND i.spent_output_index = o.index
      GROUP BY sd, cd""",
 "refresh_created (last 14 days of outputs)":
  f"""SELECT DATE(block_timestamp) d, SUM(value) v, COUNT(*) n FROM {D}.outputs`
      WHERE block_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY) GROUP BY d""",
 "refresh_spent (last 14 days of inputs x all outputs)":
  f"""SELECT DATE(i.block_timestamp) sd, DATE(o.block_timestamp) cd, SUM(i.value) v, COUNT(*) n
      FROM {D}.inputs` i JOIN {D}.outputs` o
      ON i.spent_transaction_hash = o.transaction_hash AND i.spent_output_index = o.index
      WHERE i.block_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
      GROUP BY sd, cd""",
}

c = bigquery.Client(project=PROJECT)
cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
out, lines = {}, []
for name, sql in Q.items():
    try:
        b = c.query(sql, job_config=cfg).total_bytes_processed
        out[name] = b
        lines.append(f"{b/TIB:7.3f} TiB  ({b/1e9:8.1f} GB)  {name}")
    except Exception as e:
        out[name] = None
        lines.append(f"ERROR  {name}: {str(e)[:300]}")
b = [v for k, v in out.items() if k.startswith("backfill") and v]
if len(b) == 2:
    lines.append(f"{sum(b)/TIB:7.3f} TiB  ({sum(b)/1e9:8.1f} GB)  BACKFILL TOTAL (one time)")
r = [v for k, v in out.items() if k.startswith("refresh") and v]
if len(r) == 2:
    lines.append(f"{sum(r)/TIB:7.3f} TiB  ({sum(r)/1e9:8.1f} GB)  ONE REFRESH TOTAL")
print("\n".join(lines))
s = os.environ.get("GITHUB_STEP_SUMMARY")
if s:
    open(s, "a").write("## BigQuery dry run (nothing was scanned)\n```\n" + "\n".join(lines) + "\n```\n")
