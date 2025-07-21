from pymongo import MongoClient
from bson import ObjectId

client = MongoClient("mongodb://localhost:27017/")
db = client["dental_app"]
patients_col = db["patients"]
treatments_col = db["treatments_col"]

updated_count = 0

# Loop through treatments with placeholder patient_id
for treatment in treatments_col.find({"patient_id": "replace_this_with_real_patient_id"}):
    print(f"🔍 Found treatment with placeholder ID: {treatment['_id']}")

    # Attempt to match with John Doe (case-insensitive)
    patient = patients_col.find_one({
        "first_name": {"$regex": "^John$", "$options": "i"},
        "last_name": {"$regex": "^Doe$", "$options": "i"}
    })

    if patient:
        treatments_col.update_one(
            {"_id": treatment["_id"]},
            {"$set": {"patient_id": patient["_id"]}}
        )
        print(
            f"✅ Updated treatment {treatment['_id']} with patient_id {patient['_id']}")
        updated_count += 1
    else:
        print("❌ Could not find matching patient.")

print(f"\n✅ DONE: {updated_count} treatment(s) updated.")
