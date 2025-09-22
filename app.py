from werkzeug.middleware.proxy_fix import ProxyFix
from pymongo import MongoClient
import os
import re
from datetime import datetime, timedelta, timezone
from functools import wraps
from io import BytesIO

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    session, jsonify, send_file, Response, current_app, g
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson.objectid import ObjectId
from dotenv import load_dotenv

from auth import login_required, admin_required
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader

from utils_finance import (
    finance_totals_period, outstanding_all_time,
    series_daily, series_monthly, series_dentist,
    top_outstanding_by_patient, finance_totals_all_time,
)

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")
app.config["MONGODB_URI"] = os.environ.get(
    "MONGODB_URI")  # no localhost fallback in prod


def get_db():
    """Create/cache Mongo client lazily per worker."""
    if 'db' not in g:
        uri = app.config.get("MONGODB_URI")
        if not uri:
            raise RuntimeError("MONGODB_URI not set")
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        g._mongo_client = client
        g.db = client["tagata_dental"]
        ensure_indexes(g.db)  # safe if called multiple times
    return g.db


def ensure_indexes(db):
    users_col = db["users"]
    users_col.create_index([("email", 1)], unique=True, name="uniq_email")
    # add other indexes here as needed
    # patients_col = db["patients"]
    # patients_col.create_index([("last_name", 1), ("first_name", 1)], name="name_idx")


@app.get("/health")
def health():
    # Keep it light so the app can boot even if DB is momentarily down
    return "OK", 200


@app.get("/")
def index():
    # Optional: touch DB to verify connectivity
    try:
        db = get_db()
        return "Tagata Dental running", 200
    except Exception as e:
        return f"DB not ready: {e}", 500


@app.teardown_appcontext
def close_db(exc):
    client = getattr(g, "_mongo_client", None)
    if client:
        client.close()


# --- Clinic identity shown on invoices ---
CLINIC = {
    "name": "Tagata Dental Clinic",
    "branches": [
        {"address": "G/F, EDY Bldg., 144 Kisad Rd, Baguio City", "phone": "09081827860"},
        {"address": "Del Pillar St., Cor Burgos St., Pob 3 Pura Tarlac",
            "phone": "09195299855"},
    ],
    "Facebook": "rtagatadentalclinictarlac",
    "logo_path": "static/images/logo.png",
}


def peso(amount):
    try:
        return f"₱{float(amount):,.2f}"
    except Exception:
        return "₱0.00"


def _to_naive_utc(dt):
    """Make datetime comparable by converting tz-aware to naive UTC."""
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt  # already naive


app = Flask(__name__)


app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,  # App Platform terminates TLS; this is appropriate
)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")

# Use ONE env var name consistently
MONGO_URI = os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI (or MONGODB_URI) not set")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)

# Use your intended database name (keep it consistent!)
db = client["tagata_dental"]

# Collections
users_col = db["users"]
patients_col = db["patients"]
appointments_col = db["appointments"]
treatments_col = db["treatments"]
patient_docs_col = db["patient_docs"]
invoices_col = db["invoices"]
prescriptions_col = db["prescriptions"]

# Indexes
users_col.create_index([("email", 1)], unique=True, name="uniq_email")
treatments_col.create_index([("is_deleted", 1), ("status", 1), ("date", 1)])
treatments_col.create_index([("patient_id", 1)])
patients_col.create_index(
    [("branch", 1), ("company", 1), ("last_name", 1), ("first_name", 1)],
    name="patient_list_compound",
)
patients_col.create_index(
    [("last_name", 1), ("first_name", 1)], name="patient_name_sort")
patients_col.create_index([("contact", 1)], name="contact_exact")

# Optional: confirm connectivity
try:
    client.admin.command("ping")
    print("MongoDB ping: OK")
except Exception as e:
    print("MongoDB ping failed:", e)

# ---- Branch config ----
ALLOWED_BRANCHES = {"Baguio", "Tarlac"}   # add more if you open new branches
DEFAULT_BRANCH = "Baguio"


def normalize_branch(raw):
    """Return a safe branch value; fallback to DEFAULT_BRANCH when invalid/empty."""
    if not raw:
        return DEFAULT_BRANCH
    raw = str(raw).strip()
    return raw if raw in ALLOWED_BRANCHES else DEFAULT_BRANCH


UPLOAD_FOLDER = os.environ.get(
    "UPLOAD_FOLDER", os.path.join(app.root_path, "uploads"))
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".pdf", ".svg", ".webp", ".dcm"}


def allowed_file(filename: str) -> bool:
    _, ext = os.path.splitext(filename.lower())
    return ext in ALLOWED_EXTS


@app.route("/users")
@admin_required
def users_list():
    q = (request.args.get("q") or "").strip()
    filt = {}
    if q:
        filt = {"email": {"$regex": re.escape(q), "$options": "i"}}

    users = list(users_col.find(filt).sort([("role", 1), ("email", 1)]))
    return render_template("users.html", users=users, q=q)


@app.route("/users/<user_id>/toggle-active", methods=["POST"])
@admin_required
def users_toggle_active(user_id):
    try:
        oid = ObjectId(user_id)
    except Exception:
        flash("Invalid user id.", "danger")
        return redirect(url_for("users_list"))

    user = users_col.find_one({"_id": oid})
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("users_list"))

    new_val = not bool(user.get("is_active", True))
    users_col.update_one({"_id": oid}, {"$set": {"is_active": new_val}})
    flash(f"User {'enabled' if new_val else 'disabled'}.", "success")
    return redirect(url_for("users_list"))


@app.route("/register_user", methods=["GET", "POST"])
@admin_required
def register_user():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        role = (request.form.get("role") or "dentist").strip().lower()

        if not email or "@" not in email or "." not in email:
            flash("Please enter a valid email.", "danger")
            return redirect(url_for("register_user"))
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return redirect(url_for("register_user"))
        if role not in {"admin", "dentist", "user"}:
            flash("Invalid role.", "danger")
            return redirect(url_for("register_user"))

        hashed_password = generate_password_hash(password)

        try:
            users_col.insert_one({
                "email": email,
                "password": hashed_password,
                "role": role,
                "is_active": True,
                "created_at": datetime.utcnow(),
            })
        except DuplicateKeyError:
            flash("Email already registered.", "danger")
            return redirect(url_for("register_user"))

        flash("User registered successfully.", "success")
        return redirect(url_for("users_list"))

    return render_template("register_user.html")


@app.route("/")
def home():
    return redirect(url_for("dashboard"))


def get_current_user():
    """Fetch the logged-in user document using session['user'] structure."""
    u = session.get("user")
    if not isinstance(u, dict):
        return None

    # Prefer ObjectId if possible
    uid = str(u.get("id") or "")
    if ObjectId.is_valid(uid):
        doc = users_col.find_one({"_id": ObjectId(uid)})
        if doc:
            return doc

    # Fallbacks: by email or by string _id
    email = u.get("email")
    if email:
        doc = users_col.find_one({"email": email})
        if doc:
            return doc

    if uid:
        doc = users_col.find_one({"_id": uid})
        if doc:
            return doc

    return None


# ---------- Account Settings (change username) ----------


@app.route("/account/settings", methods=["GET", "POST"])
@login_required
def account_settings():
    user = get_current_user()
    if not user:
        flash("User not found or not logged in.", "danger")
        return redirect(url_for("login"))

    if request.method == "POST":
        new_email = (request.form.get("new_username")
                     or "").strip()  # reuse field name
        if not new_email:
            flash("Email cannot be empty.", "danger")
            return redirect(url_for("account_settings"))

        # basic sanity
        if "@" not in new_email or "." not in new_email:
            flash("Please enter a valid email.", "danger")
            return redirect(url_for("account_settings"))

        # ensure unique
        exists = users_col.find_one(
            {"email": new_email, "_id": {"$ne": user["_id"]}})
        if exists:
            flash("That email is already registered.", "danger")
            return redirect(url_for("account_settings"))

        users_col.update_one({"_id": user["_id"]}, {
                             "$set": {"email": new_email}})
        # update session copy
        su = session.get("user", {})
        su["email"] = new_email
        session["user"] = su

        flash("Email updated successfully.", "success")
        return redirect(url_for("account_settings"))

    return render_template("account_settings.html", user=user)


# ---------- Change Password (self-service) ----------


@app.route("/account/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    user = get_current_user()
    if not user:
        flash("User not found or not logged in.", "danger")
        return redirect(url_for("login"))

    if request.method == "POST":
        old_password = request.form.get("old_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        # basic checks
        if not old_password or not new_password:
            flash("Please fill in all fields.", "danger")
            return redirect(url_for("change_password"))
        if new_password != confirm_password:
            flash("New password and confirmation do not match.", "danger")
            return redirect(url_for("change_password"))
        if len(new_password) < 8:
            flash("New password must be at least 8 characters.", "danger")
            return redirect(url_for("change_password"))

        # verify old -> then set new
        if not check_password_hash(user.get("password", ""), old_password):
            flash("Old password is incorrect.", "danger")
            return redirect(url_for("change_password"))

        hashed = generate_password_hash(new_password)
        users_col.update_one({"_id": user["_id"]}, {
                             "$set": {"password": hashed}})
        flash("Password updated successfully.", "success")
        return redirect(url_for("account_settings"))

    return render_template("change_password.html", user=user)

# ---------- (Optional) Admin reset password for any user ----------


@app.route("/admin/users/<user_id>/reset-password", methods=["GET", "POST"])
@login_required
@admin_required
def admin_reset_password(user_id):

    try:
        oid = ObjectId(user_id)
    except Exception:
        flash("Invalid user id.", "danger")
        return redirect(url_for("dashboard"))

    target = users_col.find_one({"_id": oid})
    if not target:
        flash("User not found.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not new_password:
            flash("Please provide a new password.", "danger")
            return redirect(url_for("admin_reset_password", user_id=user_id))
        if new_password != confirm_password:
            flash("New password and confirmation do not match.", "danger")
            return redirect(url_for("admin_reset_password", user_id=user_id))
        if len(new_password) < 8:
            flash("New password must be at least 8 characters.", "danger")
            return redirect(url_for("admin_reset_password", user_id=user_id))

        hashed = generate_password_hash(new_password)
        users_col.update_one({"_id": oid}, {"$set": {"password": hashed}})
        flash(
            f"Password reset for user {target.get('username', '(no name)')}.", "success")
        return redirect(url_for("dashboard"))

    return render_template("admin_reset_password.html", target=target)


@app.context_processor
def utility_processor():
    def safe_url_for(endpoint, **values):
        # Only build if the endpoint exists; fallback to "#"
        if endpoint in current_app.view_functions:
            return url_for(endpoint, **values)
        return "#"
    return dict(safe_url_for=safe_url_for)


@app.route("/dashboard")
@login_required
def dashboard():
    # Dates for today
    today_dt = datetime.today()
    today_str = today_dt.strftime("%Y-%m-%d")
    current_date = today_dt.strftime("%b %d, %Y")

    # Top counters
    total_patients = patients_col.count_documents({})
    appointments_today = list(appointments_col.find({"date": today_str}))
    upcoming_count = appointments_col.count_documents(
        {"date": {"$gt": today_str}})
    recall_count = patients_col.count_documents({"recall": True})

    # Birthdays today (robust to empty/missing/strings)
    birthdays_count = patients_col.count_documents({
        "birthdate": {"$exists": True, "$ne": ""},
        "$expr": {
            "$let": {
                "vars": {
                    "bd": {
                        "$cond": [
                            {"$eq": [{"$type": "$birthdate"}, "date"]},
                            "$birthdate",
                            {
                                "$dateFromString": {
                                    "dateString": "$birthdate",
                                    "format": "%Y-%m-%d",  # change if your stored strings differ
                                    "onError": None,
                                    "onNull": None
                                }
                            }
                        ]
                    }
                },
                "in": {
                    "$and": [
                        {"$ne": ["$$bd", None]},
                        {"$eq": [{"$month": "$$bd"}, today_dt.month]},
                        {"$eq": [{"$dayOfMonth": "$$bd"}, today_dt.day]}
                    ]
                }
            }
        }
    })

    # ---------- FINANCE (single source of truth) ----------
    MATCH_ALL = {"is_deleted": {"$ne": True}, "status": {"$ne": "void"}}

    # All-time totals for Welcome finance card
    totals_pipe = [
        {"$match": MATCH_ALL},
        {"$group": {
            "_id": None,
            "total_fee":  {"$sum": {"$ifNull": ["$fee", 0]}},
            "total_paid": {"$sum": {"$ifNull": ["$paid", 0]}},
        }}
    ]
    sums = list(treatments_col.aggregate(totals_pipe))
    total_fee = (sums[0]["total_fee"] if sums else 0) or 0
    total_paid = (sums[0]["total_paid"] if sums else 0) or 0
    pending_balance = max(total_fee - total_paid, 0)

    # A/R (Outstanding) by patient – objectId-safe + non-negative balances
    ar_pipe = [
        {"$match": MATCH_ALL},
        {"$project": {
            "pid": {
                "$cond": [
                    {"$eq": [{"$type": "$patient_id"}, "objectId"]},
                    "$patient_id",
                    {"$toObjectId": "$patient_id"}
                ]
            },
            "bal": {"$max": [
                {"$subtract": [
                    {"$ifNull": ["$fee", 0]},
                    {"$ifNull": ["$paid", 0]}
                ]},
                0
            ]}
        }},
        {"$group": {"_id": "$pid", "balance": {"$sum": "$bal"}}},
        {"$match": {"balance": {"$gt": 0}}},
        {"$group": {"_id": None, "patients": {"$sum": 1}, "total": {"$sum": "$balance"}}}
    ]
    ar = list(treatments_col.aggregate(ar_pipe))
    ar_patients = ar[0]["patients"] if ar else 0
    ar_total = ar[0]["total"] if ar else 0

    return render_template(
        "dashboard.html",
        current_date=current_date,
        total_patients=total_patients,
        appointments_today=appointments_today,
        upcoming_count=upcoming_count,
        recall_count=recall_count,
        birthdays_count=birthdays_count,
        total_fee=total_fee,           # all-time billed
        total_paid=total_paid,         # all-time collected
        pending_balance=pending_balance,
        ar_patients=ar_patients,       # patients with outstanding > 0
        ar_total=ar_total,             # total outstanding all-time
    )


@app.route("/patients")
@login_required
def list_patients():
    # --- inputs (with sane limits) ---
    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = min(max(int(request.args.get("per_page", 25) or 25), 5), 100)
    q = (request.args.get("q") or "").strip()
    branch = (request.args.get("branch") or "").strip()
    company = (request.args.get("company") or "").strip()

    # --- build query ---
    query = {}
    if branch:
        query["branch"] = branch
    if company:
        query["company"] = company
    if q:
        safe = re.escape(q)
        # anchored regex helps use index for prefix matches
        query["$or"] = [
            {"first_name": {"$regex": f"^{safe}", "$options": "i"}},
            {"last_name":  {"$regex": f"^{safe}", "$options": "i"}},
        ]

    # --- counts & data ---
    total = patients_col.count_documents(query)

    projection = {
        "first_name": 1, "last_name": 1, "contact": 1, "branch": 1,
        "company": 1, "last_updated": 1, "last_procedure": 1, "procedure_date": 1,
        "has_allergy": 1, "allergies": 1,
    }

    cursor = (patients_col.find(query, projection=projection)
              .sort([("last_name", 1), ("first_name", 1)])
              .skip((page - 1) * per_page)
              .limit(per_page))
    patients = list(cursor)

    # dropdown data (distinct companies; skip blanks)
    companies = sorted([c for c in patients_col.distinct("company") if c])

    return render_template(
        "patients.html",
        patients=patients,
        page=page, per_page=per_page, total=total,
        q=q, branch=branch, company=company,
        companies=companies,
    )


ALLOWED_BRANCHES = {"Baguio", "Tarlac"}


def _parse_allergies(has_allergy_val, allergies_raw):
    """Return (has_allergy: bool, allergies_list: list[str])"""
    has = (has_allergy_val == "on") or (has_allergy_val is True)
    raw = (allergies_raw or "").strip()
    items = [s.strip() for s in raw.replace(";", ",").split(",") if s.strip()]
    items = list(dict.fromkeys(items))  # remove duplicates, keep order
    return has, (items if has else [])


@app.route("/add", methods=["GET", "POST"])
@login_required
def add_patient():
    if request.method == "POST":
        # DEBUG: see exactly what was posted
        print("📝 add_patient form:", request.form.to_dict(flat=False))

        # --- Branch handling ---
        branch_vals = request.form.getlist("branch")
        branch = (branch_vals[-1] if branch_vals else "").strip()
        if branch not in ALLOWED_BRANCHES:
            flash("Please choose a valid clinic branch.", "danger")
            return redirect(url_for("add_patient"))

        # --- Allergy handling ---
        has_allergy, allergies_list = _parse_allergies(
            request.form.get("has_allergy"),
            request.form.get("allergies"),
        )

        # --- Build new patient doc ---
        new_patient = {
            "branch": branch,
            "first_name": (request.form.get("first_name") or "").strip(),
            "last_name": (request.form.get("last_name") or "").strip(),
            "middle_name": (request.form.get("middle_name") or "").strip(),
            "birthdate": (request.form.get("birthdate") or "").strip(),
            "gender": (request.form.get("gender") or "").strip(),
            "contact": (request.form.get("contact") or "").strip(),
            "address": (request.form.get("address") or "").strip(),
            "status": (request.form.get("status") or "Active").strip(),
            "referred_by": (request.form.get("referred_by") or "").strip(),
            "identification_no": (request.form.get("identification_no") or "").strip(),
            "company": (request.form.get("company") or "").strip(),

            # ✅ allergies stored normalized
            "has_allergy": has_allergy,
            "allergies": allergies_list,
        }

        patients_col.insert_one(new_patient)
        flash("Patient added.", "success")
        return redirect(url_for("list_patients"))

    return render_template("add_patient.html", branches=["Baguio", "Tarlac"])


@app.route("/appointments/add", methods=["GET", "POST"])
@login_required
def add_appointment():
    # Always compute this first so it's available for GET render
    suggested_name = (request.args.get("patient_name", "") or "").strip()

    if request.method == "POST":
        appointment = {
            "patient_name": request.form["patient_name"],
            "dentist": request.form["dentist"],
            "date": request.form["date"],
            "time": request.form["time"],
            "note": request.form["note"],
            "status": "Pending",
        }
        appointments_col.insert_one(appointment)
        return redirect(url_for("dashboard"))

    # Nice to sort for the dropdown
    patients = patients_col.find().sort([("last_name", 1), ("first_name", 1)])
    return render_template(
        "add_appointment.html",
        patients=patients,
        suggested_name=suggested_name,
    )


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

    # Fetch treatments
    raw_treatments = list(
        treatments_col.find({"patient_id": ObjectId(patient_id)})
    )

    treatments = []
    for t in raw_treatments:
        t = dict(t)  # copy so we can modify
        t["fee"] = _to_float(t.get("fee", 0))
        t["paid"] = _to_float(t.get("paid", 0))

        # Try your existing parser first
        pd = _parse_date(t.get("date"))

        # If none, fallback to ObjectId generation_time
        if not pd and isinstance(t.get("_id"), ObjectId):
            pd = t["_id"].generation_time

        # Normalize: make tz-aware -> naive UTC
        t["parsed_date"] = _to_naive_utc(pd)

        treatments.append(t)

    # ✅ Safe sort: all keys are naive or fallback
    treatments.sort(key=lambda x: x.get("parsed_date")
                    or datetime.min, reverse=True)

    recent_treatment = treatments[0] if treatments else None

    # Totals
    total_fee = sum(t["fee"] for t in treatments)
    total_paid = sum(t["paid"] for t in treatments)
    balance = {
        "total_fee": total_fee,
        "total_paid": total_paid,
        "remaining": max(total_fee - total_paid, 0.0),
    }

    # Documents (optional)
    try:
        patient_docs = list(
            patient_docs_col.find(
                {"patient_id": ObjectId(patient_id)}
            ).sort("uploaded_at", -1)
        )
    except NameError:
        patient_docs = []

    return render_template(
        "view_patient.html",
        patient=patient,
        treatments=treatments,
        recent_treatment=recent_treatment,
        balance=balance,
        patient_docs=patient_docs,
    )


@app.route("/patient/<patient_id>/upload", methods=["POST"])
@login_required
def upload_patient_doc(patient_id):
    file = request.files.get("file")
    if not file or file.filename.strip() == "":
        flash("No file selected.", "danger")
        return redirect(url_for("view_patient", patient_id=patient_id))

    if not allowed_file(file.filename):
        flash("File type not allowed.", "danger")
        return redirect(url_for("view_patient", patient_id=patient_id))

    fname = secure_filename(file.filename)
    stored_name = f"{patient_id}_{int(datetime.utcnow().timestamp())}_{fname}"
    path = os.path.join(UPLOAD_FOLDER, stored_name)
    file.save(path)

    doc = {
        "patient_id": ObjectId(patient_id),
        "original_name": fname,
        "stored_name": stored_name,
        "path": path,
        "content_type": file.mimetype,
        "uploaded_at": datetime.utcnow(),
    }
    patient_docs_col.insert_one(doc)
    flash("Document uploaded.", "success")
    return redirect(url_for("view_patient", patient_id=patient_id))


@app.route("/patient/doc/<doc_id>/download")
@login_required
def download_patient_doc(doc_id):
    doc = patient_docs_col.find_one({"_id": ObjectId(doc_id)})
    if not doc or not os.path.exists(doc.get("path", "")):
        flash("File not found.", "danger")
        pid = str(doc["patient_id"]) if doc and doc.get("patient_id") else None
        return redirect(url_for("view_patient", patient_id=pid)) if pid else redirect(url_for("list_patients"))
    return send_file(doc["path"], as_attachment=True, download_name=doc["original_name"])


@app.route("/patient/doc/<doc_id>/delete", methods=["POST"])
@login_required
def delete_patient_doc(doc_id):
    doc = patient_docs_col.find_one({"_id": ObjectId(doc_id)})
    if not doc:
        flash("Document not found.", "danger")
        return redirect(url_for("list_patients"))

    # best-effort file removal
    try:
        if os.path.exists(doc.get("path", "")):
            os.remove(doc["path"])
    except Exception:
        pass

    pid = str(doc["patient_id"]) if doc.get("patient_id") else None
    patient_docs_col.delete_one({"_id": ObjectId(doc_id)})
    flash("Document deleted.", "success")
    return redirect(url_for("view_patient", patient_id=pid)) if pid else redirect(url_for("list_patients"))


def _to_float(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _parse_date(d):
    """Normalize date for sorting."""
    if isinstance(d, datetime):
        return d
    if not d:
        return None
    if hasattr(d, "isoformat"):  # date object
        try:
            return datetime(d.year, d.month, d.day)
        except Exception:
            return None
    if isinstance(d, str):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(d.strip(), fmt)
            except ValueError:
                continue
    return None


@app.route("/update_medical_history/<patient_id>", methods=["POST"])
@login_required
def update_medical_history(patient_id):
    medical_history = {f"q{i}": request.form.get(
        f"q{i}", "") for i in range(1, 8)}
    patients_col.update_one({"_id": ObjectId(patient_id)}, {
                            "$set": {"medical_history": medical_history}})
    flash("Medical history updated successfully.", "success")
    return redirect(url_for("view_patient", patient_id=patient_id, _anchor="medical"))


@app.route("/edit/<patient_id>", methods=["GET", "POST"])
@login_required
def edit_patient(patient_id):
    patient = patients_col.find_one({"_id": ObjectId(patient_id)})
    if not patient:
        flash("Patient not found.", "danger")
        return redirect(url_for("list_patients"))

    if request.method == "POST":
        # Debug: see exactly what was posted
        print("📝 edit_patient form:", request.form.to_dict(flat=False))

        # --- Branch normalization (yours, kept) ---
        def _normalize_branch(selected_values, current_value=None):
            try:
                allowed = ALLOWED_BRANCHES
                default = DEFAULT_BRANCH
            except Exception:
                allowed = {"Baguio", "Tarlac"}
                default = "Baguio"

            vals = [v.strip() for v in selected_values if v and v.strip()]
            chosen = vals[-1] if vals else (current_value or "")
            if chosen in allowed:
                return chosen
            return current_value if (current_value in allowed) else default

        branch = _normalize_branch(
            request.form.getlist("branch"), patient.get("branch"))

        # --- Allergies normalization (NEW) ---
        has_allergy, allergies_list = _parse_allergies(
            request.form.get("has_allergy"),
            request.form.get("allergies"),
        )

        update_doc = {
            "branch": branch,

            "first_name": (request.form.get("first_name") or "").strip(),
            "last_name": (request.form.get("last_name") or "").strip(),
            "middle_name": (request.form.get("middle_name") or "").strip(),
            "birthdate": (request.form.get("birthdate") or "").strip(),
            "gender": (request.form.get("gender") or "").strip(),
            "contact": (request.form.get("contact") or "").strip(),
            "address": (request.form.get("address") or "").strip(),

            "company": (request.form.get("company") or "").strip(),
            "status": (request.form.get("status") or "Active"),
            "referred_by": (request.form.get("referred_by") or "").strip(),

            # ✅ allergies saved in normalized shape
            "has_allergy": has_allergy,
            "allergies": allergies_list,

            "profession": (request.form.get("profession") or "").strip(),
            "identification": (request.form.get("identification") or "").strip(),
            "emergency_contact": (request.form.get("emergency_contact") or "").strip(),
            "emergency_number": (request.form.get("emergency_number") or "").strip(),

            "updated_at": datetime.utcnow(),
        }

        patients_col.update_one({"_id": patient["_id"]}, {"$set": update_doc})
        flash("Patient record updated successfully!", "success")
        return redirect(url_for("view_patient", patient_id=patient_id))

    return render_template("edit_patient.html", patient=patient, branches=["Baguio", "Tarlac"])


@app.route("/delete_patient/<patient_id>", methods=["POST"])
@admin_required
def delete_patient(patient_id):
    patients_col.delete_one({"_id": ObjectId(patient_id)})
    flash("Patient record deleted successfully!", "success")
    return redirect(url_for("list_patients"))


@app.route("/add_treatment/<patient_id>", methods=["POST"])
@login_required
def add_treatment(patient_id):
    dates = request.form.getlist("date[]")
    diagnoses = request.form.getlist("diagnosis[]")
    treatments = request.form.getlist("treatment[]")
    fees = request.form.getlist("fee[]")
    paids = request.form.getlist("paid[]")

    for i in range(len(dates)):
        treatment = {
            "patient_id": ObjectId(patient_id),
            "date": dates[i],
            "diagnosis": diagnoses[i],
            "treatment": treatments[i],
            "fee": float(fees[i]),
            "paid": float(paids[i])
        }
        db.treatments.insert_one(treatment)

    flash("Treatment records added.", "success")
    return redirect(url_for("view_patient", patient_id=patient_id, _anchor="treatment"))


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
    query = {}
    patient_filter = request.args.get("patient_name", "").strip()
    date_filter = request.args.get("date", "").strip()

    if date_filter:
        query["date"] = date_filter
    if patient_filter:
        # First find all matching patients
        matching_patients = patients_col.find({
            "$or": [
                {"first_name": {"$regex": patient_filter, "$options": "i"}},
                {"last_name": {"$regex": patient_filter, "$options": "i"}}
            ]
        })
        patient_ids = [p["_id"] for p in matching_patients]
        query["patient_id"] = {"$in": patient_ids}

    # Totals
    total_fee = sum(t.get("fee", 0) for t in treatments_col.find())
    total_paid = sum(t.get("paid", 0) for t in treatments_col.find())
    pending_balance = total_fee - total_paid

    treatments = []
    for t in treatments_col.find(query):
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


@app.route("/remove_treatment/<treatment_id>", methods=["POST"])
@login_required
def remove_treatment(treatment_id):
    treatment = treatments_col.find_one({"_id": ObjectId(treatment_id)})
    if not treatment:
        flash("Treatment record not found.", "danger")
        return redirect(url_for("list_patients"))

    patient_id = treatment.get("patient_id")
    treatments_col.delete_one({"_id": ObjectId(treatment_id)})
    flash("Treatment record deleted.", "success")
    return redirect(url_for("view_patient", patient_id=patient_id, _anchor="treatment"))


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


# ===============================
# Tooth Conditions API (polished)
# ===============================

VALID_CONDS = {"C", "M", "F", "Un", "PD", "Fc", "Ab", "P", "PJC", "CLA", "RRF"}


@app.route("/api/tooth-conditions/<patient_id>", methods=["GET"])
def get_tooth_conditions(patient_id):
    """
    Return patient's saved tooth conditions, filtered to valid codes only.
    Response: { "8": "C", "12": "F", ... }
    """
    doc = patients_col.find_one({"_id": ObjectId(patient_id)}, {
                                "tooth_conditions": 1}) or {}
    raw = doc.get("tooth_conditions", {}) or {}
    cleaned = {k: v for k, v in raw.items() if v in VALID_CONDS}
    return jsonify(cleaned), 200


@app.route("/save-tooth-condition/<patient_id>", methods=["POST"])
def save_tooth_condition(patient_id):
    """
    Upsert a single tooth's condition.
    Body JSON:
      { "tooth": "8", "condition": "C" }
      - If "condition" is "", null, or missing => clears that tooth entry.
      - Only codes in VALID_CONDS are accepted.
    """
    try:
        data = request.get_json(force=True) or {}
        tooth = str(data.get("tooth", "")).strip()
        condition = data.get("condition")

        if not tooth:
            return jsonify({"status": "error", "message": "Missing tooth"}), 400

        if not condition:
            # Clear this tooth
            result = patients_col.update_one(
                {"_id": ObjectId(patient_id)},
                {"$unset": {f"tooth_conditions.{tooth}": ""}}
            )
        elif condition in VALID_CONDS:
            # Save/Update this tooth
            result = patients_col.update_one(
                {"_id": ObjectId(patient_id)},
                {"$set": {f"tooth_conditions.{tooth}": condition}}
            )
        else:
            return jsonify({"status": "error", "message": "Invalid condition"}), 400

        updated = patients_col.find_one({"_id": ObjectId(patient_id)}, {
                                        "tooth_conditions": 1}) or {}
        return jsonify({
            "status": "ok",
            "modified": bool(getattr(result, "modified_count", 1)),
            "tooth_conditions": updated.get("tooth_conditions", {}) or {}
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/reset-tooth-condition/<patient_id>", methods=["POST"])
def reset_tooth_condition(patient_id):
    """
    Remove all saved conditions for this patient.
    """
    patients_col.update_one(
        {"_id": ObjectId(patient_id)},
        {"$unset": {"tooth_conditions": ""}}
    )
    return jsonify({"status": "ok"}), 200


@app.route("/update-tooth-notes/<patient_id>", methods=["POST"])
@login_required
def update_tooth_notes(patient_id):
    note = (request.form.get("tooth_notes") or "").strip()
    patients_col.update_one(
        {"_id": ObjectId(patient_id)},
        {"$set": {"tooth_notes": note}}
    )
    flash("Tooth chart note saved.", "success")
    return redirect(url_for("view_patient", patient_id=patient_id, _anchor="tooth"))


def role_required(*roles):
    """Decorator to restrict routes to specific roles (e.g., admin, dentist)."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = session.get("user", {})
            role = user.get("role")
            if role not in roles:
                flash("Not authorized for this action.", "danger")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator

# --- Helpers ---


def _to_naive_utc(dt): ...
def _parse_date(val): ...
def next_invoice_number(): ...
# 👇 add here


def _parse_ymd(s: str | None):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return None


@app.route("/invoice/<patient_id>")
@login_required
@role_required("admin", "dentist")
def invoice_html(patient_id):
    patient = patients_col.find_one({"_id": ObjectId(patient_id)})
    if not patient:
        flash("Patient not found.", "danger")
        return redirect(url_for("list_patients"))

    # Optional filters: ?from=YYYY-MM-DD&to=YYYY-MM-DD
    from_str = request.args.get("from", "")
    to_str = request.args.get("to", "")
    d_from = _parse_ymd(from_str)
    d_to = _parse_ymd(to_str)

    q = {"patient_id": ObjectId(patient_id)}
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = d_from
        if d_to:
            q["date"]["$lte"] = d_to

    treatments = list(treatments_col.find(q).sort("date", 1))

    def to_float(x):
        try:
            return float(x)
        except:
            return 0.0

    total_fee = sum(to_float(t.get("fee", 0)) for t in treatments)
    total_paid = sum(to_float(t.get("paid", 0)) for t in treatments)
    balance = total_fee - total_paid

    return render_template(
        "invoice.html",
        clinic=CLINIC,
        patient=patient,
        treatments=treatments,
        total_fee=total_fee,
        total_paid=total_paid,
        balance=balance,
        today=datetime.today().strftime("%b %d, %Y"),
        date_from=from_str,
        date_to=to_str,
        peso=peso,  # pass helper if you want to use it in template
    )


@app.route("/invoice/pdf/<patient_id>")
@login_required
@role_required("admin", "dentist")
def invoice_pdf(patient_id):
    # --- fetch patient ---
    patient = patients_col.find_one({"_id": ObjectId(patient_id)})
    if not patient:
        flash("Patient not found.", "danger")
        return redirect(url_for("list_patients"))

    # --- fetch all treatments (you can add date-range support later) ---
    treatments = list(
        treatments_col.find(
            {"patient_id": ObjectId(patient_id)}).sort("date", 1)
    )

    def to_float(v):
        try:
            return float(v)
        except Exception:
            return 0.0

    total_fee = sum(to_float(t.get("fee", 0)) for t in treatments)
    total_paid = sum(to_float(t.get("paid", 0)) for t in treatments)
    balance = total_fee - total_paid

    # --- assign invoice number + save snapshot ---
    created_at = datetime.utcnow()
    # requires ReturnDocument import + counters collection
    invoice_no = next_invoice_number()
    inv_doc = {
        "patient_id": ObjectId(patient_id),
        "invoice_no": invoice_no,
        "created_at": created_at,
        "treatments": treatments,   # snapshot
        "total_fee": total_fee,
        "total_paid": total_paid,
        "balance": balance,
        "clinic": CLINIC,
    }
    invoices_col.insert_one(inv_doc)

    # --- PDF setup ---
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    M = 36  # margin
    y = H - M

    def peso(x):  # local peso formatter
        try:
            return f"₱{float(x):,.2f}"
        except Exception:
            return "₱0.00"

    # --- header: logo + clinic (left), invoice/date (right) ---
    # left block x positions
    left_x = M
    right_x = W - M

    # logo (optional)
    logo_w, logo_h = 64, 64
    if CLINIC.get("logo_path"):
        try:
            logo = ImageReader(CLINIC["logo_path"])
            c.drawImage(logo, left_x, y - logo_h, width=logo_w, height=logo_h,
                        preserveAspectRatio=True, mask='auto')
            text_left = left_x + logo_w + 12
        except Exception:
            text_left = left_x
    else:
        text_left = left_x

    # clinic text
    c.setFont("Helvetica-Bold", 14)
    c.drawString(text_left, y, CLINIC.get("name", ""))
    y -= 16
    c.setFont("Helvetica", 9)
    c.drawString(text_left, y, CLINIC.get("address", ""))
    y -= 12
    contact_line = CLINIC.get("phone", "")
    if CLINIC.get("email"):
        contact_line = f"{contact_line} • {CLINIC['email']}" if contact_line else CLINIC["email"]
    c.drawString(text_left, y, contact_line)

    # right block: invoice no + statement date
    c.setFont("Helvetica", 9)
    c.drawRightString(right_x, H - M, "Statement Date")
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(right_x, H - M - 14,
                      datetime.now().strftime("%b %d, %Y"))

    c.setFont("Helvetica", 9)
    c.drawRightString(right_x, H - M - 32, "Invoice No.")
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(right_x, H - M - 46, str(invoice_no))

    # move y below the logo/clinic block
    y = min(y - 14, H - M - 80)

    # patient line
    full_name = f"{patient.get('last_name', '').upper()}, {patient.get('first_name', '')}"
    company = patient.get("company") or "N/A"
    c.setFont("Helvetica-Bold", 10)
    c.drawString(M, y, "Patient:")
    c.setFont("Helvetica", 10)
    c.drawString(M + 48, y, f"{full_name}   ({company})")
    y -= 14

    # table header
    c.setFont("Helvetica-Bold", 10)
    c.line(M, y, W - M, y)
    y -= 14
    c.drawString(M, y, "Date")
    c.drawString(M + 120, y, "Treatment")
    c.drawRightString(W - M - 150, y, "Fee")
    c.drawRightString(W - M, y, "Paid")
    y -= 10
    c.line(M, y, W - M, y)
    y -= 12
    c.setFont("Helvetica", 10)

    # table rows
    for t in treatments:
        if y < 100:
            # new page with header row repeated
            c.showPage()
            y = H - M

            # (re)draw top header-less line & header row
            c.setFont("Helvetica-Bold", 10)
            c.line(M, y, W - M, y)
            y -= 14
            c.drawString(M, y, "Date")
            c.drawString(M + 120, y, "Treatment")
            c.drawRightString(W - M - 150, y, "Fee")
            c.drawRightString(W - M, y, "Paid")
            y -= 10
            c.line(M, y, W - M, y)
            y -= 12
            c.setFont("Helvetica", 10)

        fee = to_float(t.get("fee", 0))
        paid = to_float(t.get("paid", 0))
        date = t.get("date", "") or ""
        desc = str(t.get("treatment", ""))[:64]

        c.drawString(M, y, str(date))
        c.drawString(M + 120, y, desc)
        c.drawRightString(W - M - 150, y, f"{fee:,.2f}")
        c.drawRightString(W - M, y, f"{paid:,.2f}")
        y -= 14

    # totals
    y -= 6
    c.line(M, y, W - M, y)
    y -= 14
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(W - M - 150, y, f"Total Fee: {total_fee:,.2f}")
    y -= 14
    c.drawRightString(W - M - 150, y, f"Total Paid: {total_paid:,.2f}")
    y -= 14
    c.drawRightString(W - M - 150, y, f"Balance: {balance:,.2f}")
    y -= 20

    # footer
    c.setFont("Helvetica-Oblique", 9)
    c.drawCentredString(
        W / 2, y, f"Thank you for trusting {CLINIC.get('name', '')}.")
    y -= 12
    c.drawCentredString(
        W / 2, y,
        f"For inquiries, contact us at {CLINIC.get('phone', '')}"
        + (f" or {CLINIC['email']}" if CLINIC.get('email') else "")
    )

    c.showPage()
    c.save()
    pdf = buf.getvalue()
    buf.close()

    filename = f"invoice_{invoice_no}.pdf"
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


def next_invoice_number():
    """Atomically increments and returns the next invoice number."""
    doc = db.counters.find_one_and_update(
        {"_id": "invoice_no"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(doc["seq"])


@app.route("/prescription/new/<patient_id>", methods=["GET", "POST"])
@login_required
@role_required("admin", "dentist")
def prescription_new(patient_id):
    patient = patients_col.find_one({"_id": ObjectId(patient_id)})
    if not patient:
        flash("Patient not found.", "danger")
        return redirect(url_for("list_patients"))

    if request.method == "POST":
        # You can adapt field names if you like
        data = {
            "patient_id": ObjectId(patient_id),
            "created_at": datetime.now(timezone.utc),
            "doctor": session.get("user", {}).get("name") or "Attending Dentist",
            "clinic": CLINIC,
            "items": [],  # list of meds
            "notes": request.form.get("notes", "").strip(),
        }

        # items[] posted in parallel arrays
        names = request.form.getlist("med_name[]")
        dosages = request.form.getlist("dosage[]")
        frequencies = request.form.getlist("frequency[]")
        durations = request.form.getlist("duration[]")
        instructions = request.form.getlist("instructions[]")

        for i in range(len(names)):
            n = (names[i] or "").strip()
            if not n:
                continue
            data["items"].append({
                "name": n,
                "dosage": (dosages[i] or "").strip(),
                "frequency": (frequencies[i] or "").strip(),
                "duration": (durations[i] or "").strip(),
                "instructions": (instructions[i] or "").strip(),
            })

        if not data["items"]:
            flash("Please add at least one medicine.", "danger")
            return redirect(request.url)

        res = prescriptions_col.insert_one(data)
        flash("Prescription created.", "success")
        return redirect(url_for("prescription_print", prescription_id=str(res.inserted_id)))

    # GET: render form
    return render_template("prescription_form.html", patient=patient, clinic=CLINIC)


@app.route("/prescription/print/<prescription_id>")
@login_required
@role_required("admin", "dentist")
def prescription_print(prescription_id):
    rx = prescriptions_col.find_one({"_id": ObjectId(prescription_id)})
    if not rx:
        flash("Prescription not found.", "danger")
        return redirect(url_for("list_patients"))

    patient = patients_col.find_one({"_id": rx["patient_id"]})
    # Optional: ?auto=1 triggers window.print() on load
    auto = request.args.get("auto") == "1"
    return render_template("prescription_print.html",
                           rx=rx, patient=patient, auto_print=auto)


# ---------- Finance Dashboard ----------


@app.route("/reports/finance", endpoint="finance_reports")
@login_required
@role_required("admin")   # change if dentists should see it too
def finance_reports():
    # Parse ?from=YYYY-MM-DD&to=YYYY-MM-DD (default last 30 days)
    qs_from = request.args.get("from")
    qs_to = request.args.get("to")

    if not qs_to:
        qs_to = datetime.today().strftime("%Y-%m-%d")
    if not qs_from:
        qs_from = (datetime.strptime(qs_to, "%Y-%m-%d") -
                   timedelta(days=30)).strftime("%Y-%m-%d")

    # ✅ SAME calculations as /finance
    billed_period, collected_period = finance_totals_period(
        treatments_col, qs_from, qs_to)
    outstanding_total = outstanding_all_time(treatments_col)

    daily = series_daily(treatments_col, qs_from, qs_to)
    monthly = series_monthly(treatments_col, qs_from, qs_to)
    dentist = series_dentist(treatments_col, qs_from, qs_to)
    top_out = top_outstanding_by_patient(
        treatments_col, patients_col, limit=10)

    d_from_dt = datetime.strptime(qs_from, "%Y-%m-%d")
    d_to_dt = datetime.strptime(qs_to, "%Y-%m-%d")

    return render_template(
        "finance_dashboard.html",
        d_from=d_from_dt,
        d_to=d_to_dt,
        kpi_total_billed=billed_period,
        kpi_total_collected=collected_period,
        kpi_total_outstanding=outstanding_total,
        daily=daily,
        monthly=monthly,
        dentist=dentist,
        outstanding=top_out
    )


@app.route("/finance")
@login_required
def finance_dashboard():
    qs_from = request.args.get("from")
    qs_to = request.args.get("to")

    if qs_from and qs_to:
        billed, collected = finance_totals_period(
            treatments_col, qs_from, qs_to)
        daily = series_daily(treatments_col, qs_from, qs_to)
        monthly = series_monthly(treatments_col, qs_from, qs_to)
        dentist = series_dentist(treatments_col, qs_from, qs_to)
        d_from_dt = datetime.strptime(qs_from, "%Y-%m-%d")
        d_to_dt = datetime.strptime(qs_to, "%Y-%m-%d")
    else:
        billed, collected = finance_totals_all_time(treatments_col)
        daily = monthly = dentist = []
        d_from_dt = d_to_dt = None

    outstanding = outstanding_all_time(treatments_col)
    top_out = top_outstanding_by_patient(
        treatments_col, patients_col, limit=10)

    return render_template(
        "finance_dashboard.html",
        d_from=d_from_dt, d_to=d_to_dt,
        kpi_total_billed=billed,
        kpi_total_collected=collected,
        kpi_total_outstanding=outstanding,
        daily=daily, monthly=monthly, dentist=dentist,
        outstanding=top_out
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
