import logging
import sqlite3
import collections
from .utils import calculate_average, percent_diff
from oracle.settings import SCORING_DATABASE_PATH

logger = logging.getLogger(__name__)

sql = sqlite3.connect(SCORING_DATABASE_PATH)
sql.row_factory = sqlite3.Row
cur = sql.cursor()
cur.execute(
    "CREATE TABLE IF NOT EXISTS 'wallet_balance' ('epoch'	INTEGER NOT NULL, 'wallet_id'	TEXT NOT NULL, 'validator_index'	INTEGER NOT NULL, 'pubkey'	TEXT NOT NULL, 'balance'	INTEGER NOT NULL);"
)


def get_latest_epoch() -> int:
    cur.execute("SELECT epoch FROM wallet_balance ORDER BY epoch DESC LIMIT 1")
    result = cur.fetchone()
    if result is not None:
        return result[0]
    else:
        return 0


def check_epoch_exists(epoch: int) -> bool:
    cur.execute("SELECT COUNT(epoch) FROM wallet_balance WHERE epoch='%d'" % epoch)
    result = cur.fetchone()
    if result[0] > 0:
        return True
    else:
        return False


def write_validator_balance(
    epoch: int,
    wallet_id: str,
    validator_index: int,
    pubkey: str,
    balance: int,
) -> None:
    try:
        with sql:
            cur.execute(
                "INSERT INTO wallet_balance(epoch, wallet_id, validator_index, pubkey, balance) values (?, ?, ?, ?, ?)", (epoch, str(wallet_id), validator_index, str(pubkey), balance)
            )
    except sqlite3.IntegrityError as e:
        logger.error(e)


def get_effectiveness(start: int, end: int) -> dict:
    start_epoch = collections.defaultdict(dict)
    end_epoch = collections.defaultdict(dict)
    for balance in cur.execute("SELECT * FROM wallet_balance WHERE epoch='%d' ORDER BY wallet_id DESC" % start):
        wallet = str(balance['wallet_id'])
        index = str(balance['validator_index'])
        start_epoch[wallet][index] = balance = balance['balance']
    for balance in cur.execute("SELECT * FROM wallet_balance WHERE epoch='%d' ORDER BY wallet_id DESC" % end):
        wallet = str(balance['wallet_id'])
        index = str(balance['validator_index'])
        end_epoch[wallet][index] = balance = balance['balance']

    effectiveness = {}
    for operator in start_epoch.keys():
        diff = []
        for index, value in start_epoch[operator].items():
            if index in end_epoch[operator]:
                end_balance = end_epoch[operator][index]
                diff.append(percent_diff(value, end_balance))
            else:
                continue
        effectiveness[operator] = calculate_average(diff)
    return effectiveness
