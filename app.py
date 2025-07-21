from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from werkzeug.security import check_password_hash
from pymongo import MongoClient
from bson.objectid import ObjectId
from auth import login_required, admin_required
from datetime import datetime
import os
from dotenv import load_dotenv
load_dotenv()


app = Flask(__name__)
app.secret_key = "tagata-dental-2025-secret-key"

# ✅ MongoDB setup (dynamic for local + production)
mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
client = MongoClient(mongo_uri)
db = client["tagata_dental"]
users_col = db["users"]
patients_col = db["patients"]
appointments_col = db["appointments"]
treatments_col = db["treatments"]


@app.route("/")
def home():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required
def dashboard():
    today = datetime.now().strftime("%Y-%m-%d")
    current_date = datetime.now().strftime("%b %d, %Y")

    total_patients = patients_col.count_documents({})
    appointments_today = list(appointments_col.find({"date": today}))
    upcoming_count = appointments_col.count_documents({"date": {"$gt": today}})
    recall_count = patients_col.count_documents({"recall": True})

    today_dayofyear = datetime.now().timetuple().tm_yday
    birthdays_count = patients_col.count_documents({
        "$expr": {
            "$eq": [
                {"$dayOfYear": {"$toDate": "$birthdate"}},
                today_dayofyear
            ]
        }
    })

    # ✅ Finance Aggregation from treatments_col
    total_fee_cursor = treatments_col.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$fee"}}}
    ])
    total_paid_cursor = treatments_col.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$paid"}}}
    ])

    total_fee = next(total_fee_cursor, {"total": 0})["total"]
    total_paid = next(total_paid_cursor, {"total": 0})["total"]
    pending_balance = total_fee - total_paid

    return render_template(
        "dashboard.html",
        current_date=current_date,
        total_patients=total_patients,
        appointments_today=appointments_today,
        upcoming_count=upcoming_count,
        recall_count=recall_count,
        birthdays_count=birthdays_count,
        total_fee=total_fee,
        total_paid=total_paid,
        pending_balance=pending_balance
    )


@app.route("/patients")
@login_required
def list_patients():
    all_patients = list(patients_col.find())
    return render_template("patients.html", patients=all_patients)


@app.route("/add", methods=["GET", "POST"])
@login_required
def add_patient():
    if request.method == "POST":
        new_patient = {
            "first_name": request.form["first_name"],
            "last_name": request.form["last_name"],
            "middle_name": request.form.get("middle_name", ""),
            "birthdate": request.form.get("birthdate", ""),
            "gender": request.form.get("gender", ""),
            "contact": request.form.get("contact", ""),
            "address": request.form.get("address", ""),
            "notes": request.form.get("notes", ""),
            "has_allergy": request.form.get("has_allergy") == "on",
            "profession": request.form.get("profession", ""),
            "identification": request.form.get("identification", ""),
            "emergency_contact": request.form.get("emergency_contact", ""),
            "emergency_number": request.form.get("emergency_number", "")
        }
        patients_col.insert_one(new_patient)
        flash("Patient record added successfully!", "success")
        return redirect(url_for("list_patients"))
    return render_template("add_patient.html")


@app.route("/appointments/add", methods=["GET", "POST"])
@login_required
def add_appointment():
    if request.method == "POST":
        appointment = {
            "patient_name": request.form["patient_name"],
            "dentist": request.form["dentist"],
            "date": request.form["date"],
            "time": request.form["time"],
            "note": request.form["note"],
            "status": "Pending"
        }
        appointments_col.insert_one(appointment)
        return redirect(url_for("dashboard"))
    return render_template("add_appointment.html", patients=patients_col.find())


@app.route("/appointments", methods=["GET", "POST"])
@login_required
def view_appointments():
    filter_date = request.args.get("filter_date")
    if filter_date:
        appointments = appointments_col.find({"date": filter_date})
    else:
        appointments = appointments_col.find().sort("date", 1)
    return render_template("appointments.html", appointments=appointments, filter_date=filter_date)


@app.route("/cleanup")
@admin_required
def cleanup():
    result = patients_col.delete_many({
        "first_name": {"$in": ["", None]},
        "last_name": {"$in": ["", None]}
    })
    flash(f"Deleted {result.deleted_count} empty patients", "info")
    return redirect(url_for("list_patients"))


@app.route("/patient/<patient_id>")
@login_required
def view_patient(patient_id):
    patient = patients_col.find_one({"_id": ObjectId(patient_id)})
    if not patient:
        flash("Patient not found.", "danger")
        return redirect(url_for("list_patients"))

    # NEW: Load treatments from treatments_col
    treatments = list(treatments_col.find(
        {"patient_id": ObjectId(patient_id)}))

    return render_template("view_patient.html", patient=patient, treatments=treatments)


@app.route("/update_medical_history/<patient_id>", methods=["POST"])
@login_required
def update_medical_history(patient_id):
    medical_history = {f"q{i}": request.form.get(
        f"q{i}", "") for i in range(1, 8)}
    patients_col.update_one({"_id": ObjectId(patient_id)}, {
                            "$set": {"medical_history": medical_history}})
    flash("Medical history updated successfully.", "success")
    return redirect(url_for("view_patient", patient_id=patient_id, _anchor="medical"))


@app.route("/edit_patient/<patient_id>", methods=["GET", "POST"])
@login_required
def edit_patient(patient_id):
    patient = patients_col.find_one({"_id": ObjectId(patient_id)})
    if request.method == "POST":
        has_allergy = request.form.get("has_allergy") == "on"
        raw_allergies = request.form.get("allergies", "").strip()
        allergies = [a.strip() for a in raw_allergies.split(
            ",")] if has_allergy and "," in raw_allergies else raw_allergies

        updated_data = {
            "last_name": request.form.get("last_name"),
            "first_name": request.form.get("first_name"),
            "middle_name": request.form.get("middle_name"),
            "birthdate": request.form.get("birthdate"),
            "gender": request.form.get("gender"),
            "contact": request.form.get("contact"),
            "address": request.form.get("address"),
            "company": request.form.get("company"),
            "identification_no": request.form.get("identification_no"),
            "has_allergy": has_allergy,
            "allergies": allergies,
            "notes": request.form.get("notes"),
            "emergency_contact_name": request.form.get("emergency_contact_name"),
            "emergency_contact_number": request.form.get("emergency_contact_number"),
            "last_updated": datetime.now(),
        }

        patients_col.update_one({"_id": ObjectId(patient_id)}, {
                                "$set": updated_data})
        flash("Patient record updated successfully!", "success")
        return redirect(url_for("view_patient", patient_id=patient_id))
    return render_template("edit_patient.html", patient=patient)


@app.route("/delete_patient/<patient_id>", methods=["POST"])
@admin_required
def delete_patient(patient_id):
    patients_col.delete_one({"_id": ObjectId(patient_id)})
    flash("Patient record deleted successfully!", "success")
    return redirect(url_for("list_patients"))


@app.route("/add_treatment/<patient_id>", methods=["POST"])
@login_required
def add_treatment(patient_id):
    patient = patients_col.find_one({"_id": ObjectId(patient_id)})
    if not patient:
        flash("Patient not found.", "danger")
        return redirect(url_for("list_patients"))

    dates = request.form.getlist("date[]")
    diagnoses = request.form.getlist("diagnosis[]")
    treatments = request.form.getlist("treatment[]")
    fees = request.form.getlist("fee[]")
    paids = request.form.getlist("paid[]")

    new_records = []
    for i in range(len(dates)):
        try:
            fee = float(fees[i])
            paid = float(paids[i])
        except (ValueError, IndexError):
            fee = paid = 0.0

        record = {
            "patient_id": ObjectId(patient_id),
            "date": dates[i],
            "diagnosis": diagnoses[i],
            "treatment": treatments[i],
            "fee": fee,
            "paid": paid,
            "balance": fee - paid
        }
        new_records.append(record)

    if new_records:
        treatments_col.insert_many(new_records)
        flash(f"{len(new_records)} treatment record(s) added.", "success")
    else:
        flash("No records to add.", "warning")

    return redirect(url_for("view_patient", patient_id=patient_id) + "#treatment")


@app.route("/delete_treatment/<patient_id>/<int:index>", methods=["POST"])
@login_required
def delete_treatment(patient_id, index):
    patient = patients_col.find_one({"_id": ObjectId(patient_id)})
    if patient and "treatment_records" in patient:
        records = patient["treatment_records"]
        if 0 <= index < len(records):
            records.pop(index)
            patients_col.update_one({"_id": ObjectId(patient_id)}, {
                                    "$set": {"treatment_records": records}})
            flash("Treatment record deleted.", "success")
    return redirect(url_for("view_patient", patient_id=patient_id))


@app.route("/update_treatment/<patient_id>/<int:index>", methods=["POST"])
@login_required
def update_treatment(patient_id, index):
    updated_record = {
        "date": request.form["date"],
        "diagnosis": request.form["diagnosis"],
        "treatment": request.form["treatment"],
        "fee": float(request.form["fee"]),
        "paid": float(request.form["paid"]),
        "balance": float(request.form["balance"]),
    }
    patient = patients_col.find_one({"_id": ObjectId(patient_id)})
    if patient and "treatment_records" in patient:
        treatment_records = patient["treatment_records"]
        if 0 <= index < len(treatment_records):
            treatment_records[index] = updated_record
            patients_col.update_one({"_id": ObjectId(patient_id)}, {
                                    "$set": {"treatment_records": treatment_records}})
    return redirect(url_for('view_patient', patient_id=patient_id, _anchor='treatment'))


@app.route('/get_appointments')
def get_appointments():
    events = [{"title": a.get("patient_name", "Appointment"),
               "start": a["date"]} for a in appointments_col.find()]
    return jsonify(events)


@app.route("/fix_company_field")
def fix_company_field():
    result = patients_col.update_many(
        {"company": {"$exists": False}}, {"$set": {"company": ""}})
    return f"Updated {result.modified_count} patient records with missing company."


@app.route("/calendar")
@login_required
def calendar():
    return render_template("calendar.html")


@app.route('/appointments/delete/<appointment_id>', methods=['POST'])
@login_required
def delete_appointment(appointment_id):
    appointments_col.delete_one({'_id': ObjectId(appointment_id)})
    flash("Appointment deleted.", "success")
    return redirect(url_for('dashboard'))


@app.route("/appointments/mark_completed/<appointment_id>", methods=["POST"])
@login_required
def mark_completed(appointment_id):
    result = appointments_col.update_one({"_id": ObjectId(appointment_id)}, {
                                         "$set": {"status": "Completed"}})
    flash("Appointment marked as completed." if result.modified_count >
          0 else "Appointment not found or already completed.", "success" if result.modified_count > 0 else "danger")
    return redirect(url_for("dashboard"))


@app.route("/appointments/update_status/<appointment_id>", methods=["POST"])
@login_required
def update_appointment_status(appointment_id):
    new_status = request.form.get("status")
    result = appointments_col.update_one(
        {"_id": ObjectId(appointment_id)},
        {"$set": {"status": new_status}}
    )
    flash(
        f"Status updated to {new_status}." if result.modified_count > 0 else "Status update failed or unchanged.",
        "success" if result.modified_count > 0 else "warning"
    )
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        print("⛳ Email:", email)
        print("⛳ Password (entered):", password)

        user = users_col.find_one({"email": email})
        print("⛳ User found:", user)

        if user:
            password_check = check_password_hash(user["password"], password)
            print("⛳ Password hash match:", password_check)
        else:
            password_check = False

        if user and password_check:
            session["user"] = {
                "id": str(user["_id"]),
                "email": user["email"],
                "role": user.get("role", "user")
            }
            flash("Login successful!", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid credentials.", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("user", None)
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/admin-panel")
@admin_required
def admin_panel():
    return render_template("admin.html", user=session["user"])


@app.route("/register_user", methods=["GET", "POST"])
@admin_required
def register_user():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        role = request.form.get("role", "doctor")

        # Check if user already exists
        if users_col.find_one({"email": email}):
            flash("Email already registered.", "danger")
            return redirect(url_for("register_user"))

        hashed_password = generate_password_hash(password)
        users_col.insert_one({
            "email": email,
            "password": hashed_password,
            "role": role
        })
        flash("User registered successfully.", "success")
        return redirect(url_for("dashboard"))

    return render_template("register_user.html")


@app.route("/appointments/json")
def appointments_json():
    start = request.args.get("start")
    end = request.args.get("end")

    query = {}
    if start and end:
        query["date"] = {"$gte": start, "$lte": end}

    appointments = appointments_col.find(query)
    events = []

    for appt in appointments:
        events.append({
            "id": str(appt["_id"]),
            "title": f"{appt.get('patient_name', 'Unknown')} - {appt.get('status', 'Pending')}",
            "start": appt.get("date"),
            "color": get_status_color(appt.get("status", "Pending")),
            "extendedProps": {
                "note": appt.get("note", ""),
                "dentist": appt.get("dentist", ""),
                "time": appt.get("time", "")
            }
        })

    return jsonify(events)


def get_status_color(status):
    return {
        "Completed": "#28a745",  # green
        "Pending": "#ffc107",    # yellow
        "Cancelled": "#dc3545",  # red
        "No Show": "#6c757d"     # gray
    }.get(status, "#0d6efd")     # default: blue


@app.route("/appointments/view/<appointment_id>")
@login_required
def view_appointment(appointment_id):
    appointment = appointments_col.find_one({"_id": ObjectId(appointment_id)})
    if not appointment:
        flash("Appointment not found.", "danger")
        return redirect(url_for("calendar"))

    return render_template("view_appointment.html", appointment=appointment)


@app.route("/finance-report")
@login_required
def finance_report():
    total_fee_cursor = treatments_col.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$fee"}}}
    ])
    total_paid_cursor = treatments_col.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$paid"}}}
    ])

    total_fee = next(total_fee_cursor, {"total": 0})["total"]
    total_paid = next(total_paid_cursor, {"total": 0})["total"]
    pending_balance = total_fee - total_paid

    treatments = []
    for t in treatments_col.find():
        patient = patients_col.find_one({"_id": t.get("patient_id")})
        t["patient_name"] = f"{patient['first_name']} {patient['last_name']}" if patient else "Unknown"
        treatments.append(t)

    return render_template("finance_report.html",
                           total_fee=total_fee,
                           total_paid=total_paid,
                           pending_balance=pending_balance,
                           treatments=treatments)


@app.route("/add-treatment", methods=["GET", "POST"])
@login_required
def add_new_treatment():
    if request.method == "POST":
        treatment = {
            "patient_id": ObjectId(request.form["patient_id"]),
            "treatment": request.form["treatment"],
            "fee": float(request.form["fee"]),
            "paid": float(request.form["paid"]),
            "date": request.form["date"]
        }
        treatments_col.insert_one(treatment)
        flash("Treatment added successfully.", "success")
        return redirect(url_for("finance_report"))

    patients = list(patients_col.find())
    return render_template("add_treatment.html", patients=patients)


@app.route("/remove_treatment/<treatment_id>/<patient_id>", methods=["POST"])
@login_required
def remove_treatment(treatment_id, patient_id):
    treatment = treatments_col.find_one({"_id": ObjectId(treatment_id)})
    if not treatment:
        flash("Treatment record not found.", "danger")
        return redirect(url_for("view_patient", patient_id=patient_id) + "#treatment")

    treatments_col.delete_one({"_id": ObjectId(treatment_id)})
    flash("Treatment record deleted.", "success")
    return redirect(url_for("view_patient", patient_id=patient_id) + "#treatment")


@app.route("/update_treatment_from_report/<treatment_id>", methods=["POST"])
@login_required
def update_treatment_from_report(treatment_id):
    updated_data = {
        "treatment": request.form["treatment"],
        "fee": float(request.form["fee"]),
        "paid": float(request.form["paid"])
    }
    treatments_col.update_one({"_id": ObjectId(treatment_id)}, {
                              "$set": updated_data})
    flash("Treatment updated successfully.", "success")
    return redirect(url_for("finance_report"))


@app.route("/update_treatment_inline/<treatment_id>", methods=["POST"])
@login_required
def update_treatment_inline(treatment_id):
    updated_data = {
        "diagnosis": request.form["diagnosis"],
        "treatment": request.form["treatment"],
        "fee": float(request.form["fee"]),
        "paid": float(request.form["paid"])
    }
    treatments_col.update_one({"_id": ObjectId(treatment_id)}, {
                              "$set": updated_data})
    flash("Treatment updated successfully.", "success")

    # Get patient ID to redirect back to patient profile
    treatment = treatments_col.find_one({"_id": ObjectId(treatment_id)})
    return redirect(url_for("view_patient", patient_id=treatment["patient_id"], _anchor="treatment"))


@app.route("/create-admin")
def create_admin():
    if users_col.find_one({"email": "admin@example.com"}):
        return "👤 Admin already exists"

    users_col.insert_one({
        "email": "admin@example.com",
        "password": generate_password_hash("admin123"),
        "role": "admin"
    })
    return "✅ Admin created"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
