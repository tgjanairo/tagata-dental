from pymongo import MongoClient

# Connect to MongoDB
client = MongoClient("mongodb://localhost:27017/")
db = client["tagata_dental"]
patients_col = db["patients"]

# Find patients with has_allergy = true and missing/empty allergies field
patients_to_fix = patients_col.find({
    "has_allergy": True,
    "$or": [
        {"allergies": {"$exists": False}},
        {"allergies": None},
        {"allergies": ""},
        {"allergies": []}
    ]
})

# Update each patient with default allergy list
for patient in patients_to_fix:
    patients_col.update_one(
        {"_id": patient["_id"]},
        {"$set": {"allergies": ["Unspecified allergy"]}}
    )
    print(f"✅ Fixed: {patient['first_name']} {patient['last_name']}")

print("🎉 All missing allergies fixed.")
