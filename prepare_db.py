"""One-off: copy the source DB and fix the label join + add indexes (see ADR-004)."""
import shutil
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).parent.parent / "data" / "data.db"
DST = Path(__file__).parent / "fsm.db"

if DST.exists() and "--force" not in sys.argv:
	sys.exit(f"{DST} already exists (use --force to rebuild)")

shutil.copy(SRC, DST)
con = sqlite3.connect(DST)
con.executescript("""
	-- transaction_id is TEXT in the source; re-type it so joins can use the index
	CREATE TABLE fraud_labels_new (transaction_id INTEGER PRIMARY KEY, is_fraud BOOLEAN);
	INSERT INTO fraud_labels_new SELECT CAST(transaction_id AS INTEGER), is_fraud FROM fraud_labels;
	DROP TABLE fraud_labels;
	ALTER TABLE fraud_labels_new RENAME TO fraud_labels;
    
	CREATE INDEX ix_tx_card ON transactions(card_id);
	CREATE INDEX ix_tx_merchant ON transactions(merchant_id);
	CREATE INDEX ix_tx_location ON transactions(merchant_location_id);
	CREATE INDEX ix_tx_date ON transactions(date);
	ANALYZE;
""")
con.commit()
con.execute("VACUUM")
con.close()
print(f"Built {DST}")