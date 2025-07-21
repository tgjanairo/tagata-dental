from pymongo import MongoClient

client = MongoClient("mongodb://localhost:27017/")
db = client["tagata_dental"]

# Export these for app.py and auth.py
users_col = db["users"]
patients_col = db["patients"]
appointments_col = db["appointments"]
