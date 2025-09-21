# utils_finance.py
from datetime import datetime


def _match_period(d_from: str, d_to: str):
    """
    Standard match for period-based queries.
    Dates are strings 'YYYY-MM-DD' (inclusive).
    Excludes void/deleted docs.
    """
    return {
        "is_deleted": {"$ne": True},
        "status": {"$ne": "void"},
        "date": {"$gte": d_from, "$lte": d_to}
    }


def _match_all_time():
    """Standard match for all-time KPIs."""
    return {"is_deleted": {"$ne": True}, "status": {"$ne": "void"}}


def finance_totals_period(treatments_col, d_from: str, d_to: str):
    """Billed & Collected for a period."""
    pipeline = [
        {"$match": _match_period(d_from, d_to)},
        {"$group": {
            "_id": None,
            "billed":    {"$sum": {"$ifNull": ["$fee", 0]}},
            "collected": {"$sum": {"$ifNull": ["$paid", 0]}}
        }}
    ]
    res = list(treatments_col.aggregate(pipeline))
    return (res[0]["billed"], res[0]["collected"]) if res else (0, 0)


def finance_totals_all_time(treatments_col):
    """All-time billed & collected."""
    pipeline = [
        {"$match": _match_all_time()},
        {"$group": {
            "_id": None,
            "billed":    {"$sum": {"$ifNull": ["$fee", 0]}},
            "collected": {"$sum": {"$ifNull": ["$paid", 0]}}
        }}
    ]
    res = list(treatments_col.aggregate(pipeline))
    return (res[0]["billed"], res[0]["collected"]) if res else (0, 0)


def outstanding_all_time(treatments_col):
    """Sum of max(fee - paid, 0) across all valid treatments."""
    pipeline = [
        {"$match": _match_all_time()},
        {"$project": {
            "bal": {"$max": [
                {"$subtract": [
                    {"$ifNull": ["$fee", 0]},
                    {"$ifNull": ["$paid", 0]}
                ]},
                0
            ]}
        }},
        {"$group": {"_id": None, "outstanding": {"$sum": "$bal"}}}
    ]
    res = list(treatments_col.aggregate(pipeline))
    return res[0]["outstanding"] if res else 0

# ---------- series for charts ----------


def series_daily(treatments_col, d_from: str, d_to: str):
    """[{_id: 'YYYY-MM-DD', billed, collected}]"""
    pipeline = [
        {"$match": _match_period(d_from, d_to)},
        {"$group": {
            "_id": "$date",
            "billed": {"$sum": {"$ifNull": ["$fee", 0]}},
            "collected": {"$sum": {"$ifNull": ["$paid", 0]}}
        }},
        {"$sort": {"_id": 1}}
    ]
    return list(treatments_col.aggregate(pipeline))


def series_monthly(treatments_col, d_from: str, d_to: str):
    """Group by month. Emits first-day ISO 'YYYY-MM-01' for chart labels."""
    pipeline = [
        {"$match": _match_period(d_from, d_to)},
        {"$group": {
            "_id": {"$concat": [
                {"$substr": ["$date", 0, 7]}, "-01"  # 'YYYY-MM-01'
            ]},
            "billed": {"$sum": {"$ifNull": ["$fee", 0]}},
            "collected": {"$sum": {"$ifNull": ["$paid", 0]}}
        }},
        {"$sort": {"_id": 1}}
    ]
    return list(treatments_col.aggregate(pipeline))


def series_dentist(treatments_col, d_from: str, d_to: str):
    """Group by dentist/doctor field (fallback 'Unknown')."""
    pipeline = [
        {"$match": _match_period(d_from, d_to)},
        {"$group": {
            "_id": {"$ifNull": ["$dentist", "Unknown"]},
            "billed": {"$sum": {"$ifNull": ["$fee", 0]}},
            "collected": {"$sum": {"$ifNull": ["$paid", 0]}}
        }},
        {"$sort": {"billed": -1}}
    ]
    return list(treatments_col.aggregate(pipeline))


def top_outstanding_by_patient(treatments_col, patients_col, limit=10):
    """
    Returns [{name, balance}] for top outstanding.
    Handles string/ObjectId patient_id.
    """
    pipeline = [
        {"$match": _match_all_time()},
        {"$project": {
            "pid": {
                "$cond": [
                    {"$eq": [{"$type": "$patient_id"}, "objectId"]},
                    "$patient_id",
                    {"$toObjectId": "$patient_id"}
                ]
            },
            "bal": {"$max": [
                {"$subtract": [{"$ifNull": ["$fee", 0]},
                    {"$ifNull": ["$paid", 0]}]},
                0
            ]}
        }},
        {"$group": {"_id": "$pid", "balance": {"$sum": "$bal"}}},
        {"$sort": {"balance": -1}},
        {"$limit": limit},
        {"$lookup": {"from": patients_col.name,
                     "localField": "_id", "foreignField": "_id", "as": "p"}},
        {"$addFields": {"p": {"$arrayElemAt": ["$p", 0]}}},
        {"$addFields": {
            "name": {
                "$trim": {"input": {"$concat": [
                    {"$ifNull": ["$p.first_name", ""]}, " ",
                    {"$ifNull": ["$p.last_name", ""]}
                ]}}
            }
        }},
        {"$project": {"_id": 0, "name": {
            "$ifNull": ["$name", "Unknown"]}, "balance": 1}}
    ]
    return list(treatments_col.aggregate(pipeline))
