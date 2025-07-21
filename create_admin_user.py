from werkzeug.security import generate_password_hash
from pymongo import MongoClient

client = MongoClient("mongodb://localhost:27017/")
db = client["tagata_dental"]  # Make sure this matches the DB name in your app
users_col = db["users"]

users_col.insert_one({
    "email": "admin@example.com",
    "password": generate_password_hash("admin123"),
    "role": "admin"
})

print("✅ Admin user inserted.")
