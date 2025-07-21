from pymongo import MongoClient
from bson import ObjectId

# Connect to MongoDB
client = MongoClient("mongodb://localhost:27017/")
db = client["dental_app"]

patients_col = db["patients"]
treatments_col = db["treatments"]

updated = 0
skipped = []

# Loop through treatments with string-based patient_id (assumed to be a name)
for t in treatments_col.find({"patient_id": {"$type": "string"}}):
    name = t["patient_id"].strip().lower()
    treatment_id = str(t["_id"])

    # Try to find a patient matching by full name
    matched_patient = None
    for p in patients_col.find():
        full_name = f"{p.get('first_name', '').strip().lower()} {p.get('last_name', '').strip().lower()}"
        if full_name == name:
            matched_patient = p
            break

    if matched_patient:
        treatments_col.update_one(
            {"_id": t["_id"]},
            {"$set": {"patient_id": matched_patient["_id"]}}
        )
        updated += 1
        print(f"✅ Matched '{name}' → Patient ID: {matched_patient['_id']}")
    else:
        skipped.append(treatment_id)
        print(
            f"⏭️ No match found for: '{name}' (Treatment ID: {treatment_id})")

print("\n=== DONE ===")
print(f"🔄 Updated treatments: {updated}")
print(f"❌ Skipped treatments: {len(skipped)}")

if skipped:
    print("Skipped Treatment IDs:")
    for tid in skipped:
        print(f"- {tid}")
