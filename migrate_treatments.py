from pymongo import MongoClient
from bson.objectid import ObjectId

client = MongoClient("mongodb://localhost:27017/")
db = client["tagata_dental"]
patients_col = db["patients"]
treatments_col = db["treatments"]

migrated_count = 0

for patient in patients_col.find():
    patient_id = patient["_id"]
    records = patient.get("treatment_records", [])

    for record in records:
        treatment_doc = {
            "patient_id": patient_id,
            "date": record.get("date"),
            "diagnosis": record.get("diagnosis", ""),
            "treatment": record.get("treatment", ""),
            "fee": float(record.get("fee", 0)),
            "paid": float(record.get("paid", 0))
        }
        treatments_col.insert_one(treatment_doc)
        migrated_count += 1

    # Optional: clear out old embedded records
    patients_col.update_one({"_id": patient_id}, {
                            "$unset": {"treatment_records": ""}})

print(f"✅ Migration complete. {migrated_count} treatment records migrated.")
