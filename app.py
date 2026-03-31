from flask import Flask, render_template, redirect, url_for, flash, request, jsonify, session
from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from models import db, User, Income, Expense, Goal
from forms import (
    RegistrationForm,
    LoginForm,
    IncomeForm,
    ExpenseForm,
    GoalForm,
    SavingsUpdateForm,
)
from datetime import datetime, timedelta
from sqlalchemy import func
from collections import defaultdict
from werkzeug.middleware.proxy_fix import ProxyFix
import json
import os
import io
import base64
import imaplib
import email as email_lib
from email.header import decode_header
from email.utils import parsedate_to_datetime
import re
import ssl
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from bs4 import BeautifulSoup

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # type: ignore[assignment]

from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView

admin = Admin(app, name="Finance Manager")
app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET", "your-secret-key-change-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///finance.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# Add admin panel


# Protect admin views with login
class AdminModelView(ModelView):
    def is_accessible(self):
        return (
            current_user.is_authenticated and current_user.username == "admin"
        )  # Change to your admin username

    def inaccessible_callback(self, name, **kwargs):
        return redirect(url_for("login"))


# Add your models to admin
admin.add_view(AdminModelView(User, db.session))
admin.add_view(AdminModelView(Income, db.session))
admin.add_view(AdminModelView(Expense, db.session))
admin.add_view(AdminModelView(Goal, db.session))


@app.route("/create_admin")
def create_admin():
    # Check if admin already exists
    admin_user = User.query.filter_by(username="admin").first()
    if not admin_user:
        admin_password = os.environ.get("ADMIN_PASSWORD")
        if not admin_password:
            return "ADMIN_PASSWORD environment variable is not set. Admin user not created.", 500
        admin_user = User(
            username="admin", email="admin@example.com", password=admin_password
        )
        db.session.add(admin_user)
        db.session.commit()
        return "Admin user created! Username: admin"
    return "Admin user already exists"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# Create tables and run any needed column migrations
with app.app_context():
    db.create_all()
    migration_stmts = [
        "ALTER TABLE user ADD COLUMN has_seen_tutorial BOOLEAN DEFAULT 0 NOT NULL",
        "ALTER TABLE user ADD COLUMN last_monthly_reset DATETIME",
        "ALTER TABLE goal ADD COLUMN priority VARCHAR(10) NOT NULL DEFAULT '1'",
        "ALTER TABLE income ADD COLUMN created_at DATETIME",
        "ALTER TABLE income ADD COLUMN is_recurring BOOLEAN DEFAULT 0",
        "ALTER TABLE expense ADD COLUMN created_at DATETIME",
        "ALTER TABLE expense ADD COLUMN sub_start_date DATETIME",
        "ALTER TABLE expense ADD COLUMN sub_end_date DATETIME",
        "ALTER TABLE expense ADD COLUMN sub_expired_notified BOOLEAN DEFAULT 0",
        "ALTER TABLE user ADD COLUMN mpin VARCHAR(6)",
    ]
    for stmt in migration_stmts:
        try:
            with db.engine.connect() as conn:
                conn.execute(db.text(stmt))
                conn.commit()
        except Exception:
            pass
    # Migrate old text priorities (high/medium/low) to numeric
    try:
        with db.engine.connect() as conn:
            conn.execute(db.text("UPDATE goal SET priority='1' WHERE priority='high'"))
            conn.execute(db.text("UPDATE goal SET priority='2' WHERE priority='medium'"))
            conn.execute(db.text("UPDATE goal SET priority='3' WHERE priority='low'"))
            conn.commit()
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
#  IMAP Email Parsing Helpers
# ─────────────────────────────────────────────────────────────

IMAP_PRESETS = {
    "gmail":   {"host": "imap.gmail.com",           "port": 993},
    "outlook": {"host": "imap-mail.outlook.com",    "port": 993},
    "yahoo":   {"host": "imap.mail.yahoo.com",      "port": 993},
    "hotmail": {"host": "imap-mail.outlook.com",    "port": 993},
    "icloud":  {"host": "imap.mail.me.com",         "port": 993},
}

FINANCIAL_SUBJECT_KEYWORDS = [
    "transaction", "payment", "purchase", "receipt", "order", "invoice",
    "debit", "credit", "charged", "statement", "bill", "transfer",
    "alert", "notification", "confirmation", "refund", "deposit",
    "subscription", "auto-pay", "autopay", "due", "amount",
]

FINANCIAL_SENDER_KEYWORDS = [
    "bank", "paypal", "paytm", "stripe", "amazon", "netflix", "spotify",
    "apple", "google", "microsoft", "hulu", "prime", "uber", "lyft",
    "razorpay", "hdfc", "sbi", "icici", "axis", "netsuite", "venmo",
    "cashapp", "zelle", "chase", "citibank", "wells", "fargo",
]

# keyword → (main_category, sub_category)
CATEGORY_KEYWORD_MAP = [
    (["grocery", "supermarket", "safeway", "kroger", "walmart", "costco", "whole foods", "amazon fresh", "trader joe"], ("Food & Groceries", "Groceries")),
    (["restaurant", "dining", "bistro", "cafe", "diner", "eatery", "sushi", "pizza", "burger"], ("Food & Groceries", "Dining Out")),
    (["coffee", "starbucks", "dunkin", "costa"], ("Food & Groceries", "Coffee Shops")),
    (["food delivery", "doordash", "grubhub", "ubereats", "zomato", "swiggy"], ("Food & Groceries", "Food Delivery")),
    (["fast food", "mcdonald", "kfc", "subway", "domino", "taco bell", "wendy", "burger king"], ("Food & Groceries", "Fast Food")),
    (["uber", "lyft", "taxi", "rideshare", "ola", "grab"], ("Transportation", "Taxi/Rideshare")),
    (["fuel", "gas station", "petrol", "shell", "bp ", "chevron", "exxon"], ("Transportation", "Fuel")),
    (["metro", "bus", "transit", "train", "subway pass", "rail"], ("Transportation", "Public Transport")),
    (["netflix", "hulu", "disney+", "hbo", "prime video", "apple tv", "peacock", "paramount"], ("Entertainment", "Streaming Services")),
    (["spotify", "apple music", "tidal", "deezer", "pandora", "youtube music"], ("Entertainment", "Music Streaming")),
    (["gym", "fitness", "planet fitness", "equinox", "crunch"], ("Personal & Lifestyle", "Gym Membership")),
    (["electricity", "electric", "power bill"], ("Utilities", "Electricity")),
    (["water bill"], ("Utilities", "Water")),
    (["internet", "broadband", "comcast", "xfinity", "att", "verizon", "spectrum"], ("Utilities", "Internet")),
    (["mobile", "phone bill", "t-mobile", "sprint", "cricket"], ("Utilities", "Mobile Phone")),
    (["amazon", "ebay", "etsy", "shopify", "online shopping", "shop", "purchase from"], ("Shopping", "Online Shopping")),
    (["doctor", "clinic", "hospital", "medical", "health", "dental", "pharmacy", "prescription"], ("Healthcare", "Doctor Visits")),
    (["insurance", "policy", "premium"], ("Insurance", "Health Insurance")),
    (["school", "tuition", "university", "college", "course", "udemy", "coursera"], ("Education", "School Tuition")),
    (["rent", "lease", "landlord"], ("Housing", "Rent")),
    (["salary", "payroll", "wages", "paycheck"], ("Income", "Salary")),
    (["refund", "cashback", "reward"], ("Income", "Refund")),
]


def _decode_header_str(raw):
    parts = decode_header(raw or "")
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="ignore")
        else:
            result += str(part)
    return result


def _extract_text(msg):
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            decoded = payload.decode("utf-8", errors="ignore")
            if ctype == "text/plain":
                plain += decoded
            elif ctype == "text/html":
                html += decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode("utf-8", errors="ignore")
            if msg.get_content_type() == "text/html":
                html = decoded
            else:
                plain = decoded
    if html and not plain.strip():
        soup = BeautifulSoup(html, "html.parser")
        plain = soup.get_text(separator=" ", strip=True)
    return plain


def _parse_amount(text):
    patterns = [
        r'\$\s*([\d,]+\.?\d*)',
        r'USD\s+([\d,]+\.?\d*)',
        r'Rs\.?\s*([\d,]+\.?\d*)',
        r'INR\s+([\d,]+\.?\d*)',
        r'(?:amount|total|charged|debit|credit)[:\s]+(?:of\s+)?\$?\s*([\d,]+\.?\d*)',
        r'payment of\s+\$?\s*([\d,]+\.?\d*)',
        r'\b([\d,]{1,10}\.\d{2})\b',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if 0.01 <= val <= 999999:
                    return round(val, 2)
            except ValueError:
                continue
    return None


def _guess_category(subject, body):
    combined = (subject + " " + body).lower()
    for keywords, (main_cat, sub_cat) in CATEGORY_KEYWORD_MAP:
        if any(kw in combined for kw in keywords):
            return main_cat, sub_cat
    return "Other Expenses", "Miscellaneous"


def _is_income(subject, body):
    combined = (subject + " " + body).lower()
    income_signals = ["received", "credited to your account", "deposit", "refund", "cashback",
                      "salary", "payroll", "transfer received", "payment received", "reward"]
    return any(s in combined for s in income_signals)


def _is_financial_email(subject, from_addr):
    sub_lower = subject.lower()
    from_lower = from_addr.lower()
    if any(kw in sub_lower for kw in FINANCIAL_SUBJECT_KEYWORDS):
        return True
    if any(kw in from_lower for kw in FINANCIAL_SENDER_KEYWORDS):
        return True
    return False


def scan_imap_emails(host, port, email_addr, password, days=30):
    ctx = ssl.create_default_context()
    mail = imaplib.IMAP4_SSL(host, int(port), ssl_context=ctx)
    mail.login(email_addr, password)
    mail.select("INBOX")

    since_date = (datetime.utcnow() - timedelta(days=days)).strftime("%d-%b-%Y")
    _, message_ids = mail.search(None, f"SINCE {since_date}")
    msg_ids = message_ids[0].split()
    # Process most recent first, cap at 300
    msg_ids = msg_ids[-300:][::-1]

    transactions = []
    seen_ids = set()

    for mid in msg_ids:
        try:
            _, msg_data = mail.fetch(mid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)

            subject  = _decode_header_str(msg.get("Subject", ""))
            from_addr = msg.get("From", "")
            date_str  = msg.get("Date", "")
            msg_id    = msg.get("Message-ID", mid.decode()).strip("<> ")

            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            if not _is_financial_email(subject, from_addr):
                continue

            body = _extract_text(msg)
            amount = _parse_amount(subject + " " + body[:3000])
            if not amount:
                continue

            is_income = _is_income(subject, body)
            main_cat, sub_cat = _guess_category(subject, body)
            if is_income:
                main_cat, sub_cat = "Income", "Transfer/Other"

            try:
                txn_date = parsedate_to_datetime(date_str).strftime("%Y-%m-%d")
            except Exception:
                txn_date = datetime.utcnow().strftime("%Y-%m-%d")

            transactions.append({
                "msg_id":      msg_id,
                "subject":     subject[:120],
                "from":        from_addr[:100],
                "date":        txn_date,
                "amount":      amount,
                "type":        "income" if is_income else "expense",
                "main_cat":    main_cat,
                "sub_cat":     sub_cat,
                "description": subject[:200],
            })
        except Exception:
            continue

    mail.logout()
    return transactions


# ─────────────────────────────────────────────────────────────
#  End of IMAP helpers
# ─────────────────────────────────────────────────────────────

# Category hierarchy from ChatGPT
expense_categories = {
    "Housing": {
        "subcategories": [
            "Rent",
            "Mortgage",
            "Property Tax",
            "Home Maintenance",
            "Home Repairs",
            "Furniture",
            "Home Decor",
            "Security System",
            "Cleaning Services",
            "Home Insurance",
        ],
        "classification": {
            "Rent": "need",
            "Mortgage": "need",
            "Property Tax": "need",
            "Home Maintenance": "need",
            "Home Repairs": "need",
            "Furniture": "want",
            "Home Decor": "want",
            "Security System": "need",
            "Cleaning Services": "want",
            "Home Insurance": "need",
        },
    },
    "Food & Groceries": {
        "subcategories": [
            "Groceries",
            "Basic Food Staples",
            "Dining Out",
            "Fast Food",
            "Coffee Shops",
            "Food Delivery",
            "Snacks",
            "Meal Kits",
            "Specialty Foods",
        ],
        "classification": {
            "Groceries": "need",
            "Basic Food Staples": "need",
            "Dining Out": "want",
            "Fast Food": "want",
            "Coffee Shops": "want",
            "Food Delivery": "want",
            "Snacks": "want",
            "Meal Kits": "want",
            "Specialty Foods": "want",
        },
    },
    "Transportation": {
        "subcategories": [
            "Fuel",
            "Public Transport",
            "Taxi/Rideshare",
            "Vehicle Maintenance",
            "Vehicle Insurance",
            "Parking Fees",
            "Tolls",
            "Car Loan Payment",
            "Car Wash",
            "Vehicle Registration",
        ],
        "classification": {
            "Fuel": "need",
            "Public Transport": "need",
            "Taxi/Rideshare": "want",
            "Vehicle Maintenance": "need",
            "Vehicle Insurance": "need",
            "Parking Fees": "need",
            "Tolls": "need",
            "Car Loan Payment": "need",
            "Car Wash": "want",
            "Vehicle Registration": "need",
        },
    },
    "Utilities": {
        "subcategories": [
            "Electricity",
            "Water",
            "Gas",
            "Internet",
            "Mobile Phone",
            "Trash Collection",
            "Sewer Charges",
            "Streaming Bundled with Internet",
        ],
        "classification": {
            "Electricity": "need",
            "Water": "need",
            "Gas": "need",
            "Internet": "need",
            "Mobile Phone": "need",
            "Trash Collection": "need",
            "Sewer Charges": "need",
            "Streaming Bundled with Internet": "want",
        },
    },
    "Healthcare": {
        "subcategories": [
            "Doctor Visits",
            "Hospital Bills",
            "Pharmacy",
            "Health Insurance",
            "Dental Care",
            "Vision Care",
            "Mental Health Therapy",
            "Medical Equipment",
            "Health Supplements",
        ],
        "classification": {
            "Doctor Visits": "need",
            "Hospital Bills": "need",
            "Pharmacy": "need",
            "Health Insurance": "need",
            "Dental Care": "need",
            "Vision Care": "need",
            "Mental Health Therapy": "need",
            "Medical Equipment": "need",
            "Health Supplements": "want",
        },
    },
    "Education": {
        "subcategories": [
            "School Tuition",
            "College Tuition",
            "Online Courses",
            "Books",
            "School Supplies",
            "Professional Certifications",
            "Workshops",
            "Educational Software",
        ],
        "classification": {
            "School Tuition": "need",
            "College Tuition": "need",
            "Online Courses": "want",
            "Books": "need",
            "School Supplies": "need",
            "Professional Certifications": "need",
            "Workshops": "want",
            "Educational Software": "need",
        },
    },
    "Insurance": {
        "subcategories": [
            "Health Insurance",
            "Life Insurance",
            "Vehicle Insurance",
            "Home Insurance",
            "Travel Insurance",
            "Pet Insurance",
        ],
        "classification": {
            "Health Insurance": "need",
            "Life Insurance": "need",
            "Vehicle Insurance": "need",
            "Home Insurance": "need",
            "Travel Insurance": "want",
            "Pet Insurance": "want",
        },
    },
    "Personal & Lifestyle": {
        "subcategories": [
            "Clothing",
            "Shoes",
            "Haircuts",
            "Beauty Products",
            "Gym Membership",
            "Salon Services",
            "Spa",
            "Personal Care Items",
        ],
        "classification": {
            "Clothing": "need",
            "Shoes": "need",
            "Haircuts": "need",
            "Beauty Products": "want",
            "Gym Membership": "want",
            "Salon Services": "want",
            "Spa": "want",
            "Personal Care Items": "need",
        },
    },
    "Entertainment": {
        "subcategories": [
            "Movies",
            "Concerts",
            "Gaming",
            "Streaming Subscriptions",
            "Hobbies",
            "Books & Magazines",
            "Theme Parks",
            "Events & Shows",
        ],
        "classification": {
            "Movies": "want",
            "Concerts": "want",
            "Gaming": "want",
            "Streaming Subscriptions": "want",
            "Hobbies": "want",
            "Books & Magazines": "want",
            "Theme Parks": "want",
            "Events & Shows": "want",
        },
    },
    "Travel": {
        "subcategories": [
            "Flights",
            "Hotels",
            "Vacation Packages",
            "Local Travel",
            "Travel Insurance",
            "Tour Guides",
            "Resort Stay",
        ],
        "classification": {
            "Flights": "want",
            "Hotels": "want",
            "Vacation Packages": "want",
            "Local Travel": "need",
            "Travel Insurance": "want",
            "Tour Guides": "want",
            "Resort Stay": "want",
        },
    },
    "Debt & Financial Obligations": {
        "subcategories": [
            "Credit Card Payment",
            "Loan Repayment",
            "Student Loan Payment",
            "Personal Loan",
            "Bank Fees",
            "Late Fees",
        ],
        "classification": {
            "Credit Card Payment": "need",
            "Loan Repayment": "need",
            "Student Loan Payment": "need",
            "Personal Loan": "need",
            "Bank Fees": "need",
            "Late Fees": "want",
        },
    },
    "Other": {
        "subcategories": ["Other (User Input)"],
        "classification": {"Other (User Input)": "unknown"},
    },
}

# Flatten the needs keywords for backward compatibility
essential_keywords = []
for main_cat, data in expense_categories.items():
    classification_map = data.get("classification", {})
    if isinstance(classification_map, dict):
        for subcat, classification in classification_map.items():
            if classification == "need":
                essential_keywords.append(subcat.lower())
                if main_cat.lower() not in essential_keywords:
                    essential_keywords.append(main_cat.lower())


def classify_essential(main_category, sub_category, custom_category=None):
    if sub_category is None:
        return False

    if sub_category == "Other (User Input)" and custom_category:
        return classify_essential_keywords(custom_category)

    if main_category and main_category in expense_categories:
        cat_data = expense_categories[main_category]
        if sub_category in cat_data["classification"]:
            classification = cat_data["classification"][sub_category]
            if classification == "need":
                return True
            elif classification == "want":
                return False

    return classify_essential_keywords(sub_category)


def classify_essential_keywords(text):
    if text is None or text == "":
        return False
    text_lower = text.lower()
    for kw in essential_keywords:
        if kw in text_lower:
            return True
    return False


# Helper: detect recurring subscriptions
def detect_subscriptions(user_id, months_back=3):
    since_date = datetime.utcnow() - timedelta(days=30 * months_back)
    expenses = Expense.query.filter(
        Expense.user_id == user_id, Expense.date >= since_date
    ).all()

    groups = defaultdict(list)
    for exp in expenses:
        key = (
            exp.category.strip().lower(),
            exp.description.strip().lower() if exp.description else "",
        )
        groups[key].append(exp)

    seen_keys = set()
    subscriptions = []

    # First: include any expense explicitly marked as a subscription
    for exp in expenses:
        if exp.is_subscription:
            key = (
                exp.category.strip().lower(),
                exp.description.strip().lower() if exp.description else "",
            )
            if key not in seen_keys:
                seen_keys.add(key)
                items = groups[key]
                amounts = [i.amount for i in items]
                avg_amount = sum(amounts) / len(amounts)
                subscriptions.append(
                    {
                        "category": exp.category.strip(),
                        "description": exp.description or "No description",
                        "avg_amount": avg_amount,
                        "frequency": len(items),
                        "last_date": max(i.date for i in items),
                        "expenses": items,
                    }
                )

    # Second: auto-detect by repeated pattern (same category+description, 2+ times, similar amount)
    for (cat, desc), items in groups.items():
        key = (cat, desc)
        if key in seen_keys:
            continue
        if len(items) >= 2:
            amounts = [i.amount for i in items]
            avg_amount = sum(amounts) / len(amounts)
            if all(abs(a - avg_amount) <= avg_amount * 0.1 for a in amounts):
                seen_keys.add(key)
                subscriptions.append(
                    {
                        "category": cat,
                        "description": desc or "No description",
                        "avg_amount": avg_amount,
                        "frequency": len(items),
                        "last_date": max(i.date for i in items),
                        "expenses": items,
                    }
                )

    return subscriptions


# Helper: get spending reduction suggestions
def get_spending_suggestions(user_id, goal):
    # Get last 3 months of expenses
    since_date = datetime.utcnow() - timedelta(days=90)
    expenses = Expense.query.filter(
        Expense.user_id == user_id,
        Expense.date >= since_date,
        Expense.is_essential == False,  # Only non-essential expenses
    ).all()

    # Group by category and sum
    category_totals = defaultdict(float)
    for exp in expenses:
        category_totals[exp.category] += exp.amount

    # Sort by amount (highest first)
    suggestions = []
    for category, amount in sorted(
        category_totals.items(), key=lambda x: x[1], reverse=True
    )[:5]:
        monthly_avg = amount / 3  # Average monthly spending

        # Suggest reducing by 20% as a starting point
        reduction = monthly_avg * 0.2
        months_saved = (
            goal.remaining_amount / (goal.monthly_savings + reduction)
            if goal.monthly_savings > 0
            else float("inf")
        )
        original_months = goal.estimated_months

        if months_saved != float("inf"):
            time_saved = original_months - months_saved
            suggestions.append(
                {
                    "category": category,
                    "current_spending": monthly_avg,
                    "suggested_reduction": reduction,
                    "new_monthly_savings": goal.monthly_savings + reduction,
                    "months_saved": time_saved,
                    "original_months": original_months,
                    "new_months": months_saved,
                }
            )

    return suggestions


def run_monthly_reset(user):
    """Clear non-subscription expenses and non-recurring income from previous months, keeping subscriptions and recurring salary."""
    now = datetime.utcnow()
    last_reset = user.last_monthly_reset

    if last_reset is None:
        user.last_monthly_reset = now
        db.session.commit()
        return False

    if now.year == last_reset.year and now.month == last_reset.month:
        return False

    # A new month has started — clear last month's non-subscription expenses
    prev_month_start = datetime(last_reset.year, last_reset.month, 1)
    if last_reset.month == 12:
        prev_month_end = datetime(last_reset.year + 1, 1, 1)
    else:
        prev_month_end = datetime(last_reset.year, last_reset.month + 1, 1)

    # Delete non-subscription expenses from previous month
    Expense.query.filter(
        Expense.user_id == user.id,
        Expense.date >= prev_month_start,
        Expense.date < prev_month_end,
        Expense.is_subscription == False,
    ).delete()

    # Delete non-recurring income from previous month
    Income.query.filter(
        Income.user_id == user.id,
        Income.date_received >= prev_month_start,
        Income.date_received < prev_month_end,
        Income.is_recurring == False,
    ).delete()

    user.last_monthly_reset = now
    db.session.commit()
    return True


def check_subscription_expiry(user_id):
    """Check for expired or expiring subscriptions and return alerts."""
    now = datetime.utcnow()
    alerts = []

    subscriptions = Expense.query.filter(
        Expense.user_id == user_id,
        Expense.is_subscription == True,
        Expense.sub_end_date != None,
    ).all()

    for sub in subscriptions:
        days_left = (sub.sub_end_date - now).days
        if days_left < 0:
            # Expired
            if not sub.sub_expired_notified:
                alerts.append({
                    "type": "expired",
                    "name": sub.description or sub.category,
                    "id": sub.id,
                    "end_date": sub.sub_end_date,
                })
                sub.sub_expired_notified = True
                db.session.commit()
        elif days_left <= 7:
            # Expiring soon
            alerts.append({
                "type": "expiring",
                "name": sub.description or sub.category,
                "id": sub.id,
                "days_left": days_left,
                "end_date": sub.sub_end_date,
            })

    return alerts


@app.route("/remove_expired_subscription/<int:id>")
@login_required
def remove_expired_subscription(id):
    expense = Expense.query.get_or_404(id)
    if expense.user_id != current_user.id:
        flash("Unauthorized.", "danger")
        return redirect(url_for("subscriptions"))
    db.session.delete(expense)
    db.session.commit()
    flash("Subscription removed.", "success")
    return redirect(url_for("subscriptions"))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    form = RegistrationForm()
    if form.validate_on_submit():
        user = User(
            username=form.username.data,
            email=form.email.data,
            password=form.password.data,
        )
        db.session.add(user)
        db.session.commit()
        flash("Account created! You can now log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html", form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    form = LoginForm()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        mpin_input = request.form.get("mpin_input", "").strip()
        password_input = request.form.get("password", "").strip()
        user = User.query.filter_by(username=username).first()
        if user:
            if mpin_input:
                if user.mpin and user.mpin == mpin_input:
                    login_user(user)
                    if not user.has_seen_tutorial:
                        return redirect(url_for("tutorial"))
                    return redirect(url_for("dashboard"))
                else:
                    flash("Incorrect MPIN. Please try again.", "danger")
                    return render_template("login.html", form=form,
                                           prefill_username=username, show_mpin=True)
            elif password_input:
                if user.password == password_input:
                    login_user(user)
                    if not user.has_seen_tutorial:
                        return redirect(url_for("tutorial"))
                    return redirect(url_for("dashboard"))
                else:
                    flash("Incorrect password. Please try again.", "danger")
                    return render_template("login.html", form=form,
                                           prefill_username=username,
                                           show_mpin=bool(user.mpin))
        else:
            flash("No account found with that username.", "danger")
    return render_template("login.html", form=form)


@app.route("/check-mpin-status")
def check_mpin_status():
    username = request.args.get("username", "").strip()
    user = User.query.filter_by(username=username).first()
    if user:
        return jsonify({"exists": True, "has_mpin": bool(user.mpin)})
    return jsonify({"exists": False, "has_mpin": False})


@app.route("/setup-mpin", methods=["GET", "POST"])
@login_required
def setup_mpin():
    if request.method == "POST":
        new_pin = request.form.get("new_pin", "").strip()
        confirm_pin = request.form.get("confirm_pin", "").strip()
        current_password = request.form.get("current_password", "").strip()
        if len(new_pin) != 6 or not new_pin.isdigit():
            flash("MPIN must be exactly 6 digits.", "danger")
        elif new_pin != confirm_pin:
            flash("PINs do not match.", "danger")
        elif current_user.password != current_password:
            flash("Current password is incorrect.", "danger")
        else:
            current_user.mpin = new_pin
            db.session.commit()
            flash("MPIN set successfully!", "success")
            return redirect(url_for("profile"))
    return render_template("mpin_setup.html", mode="setup")


@app.route("/change-mpin", methods=["GET", "POST"])
@login_required
def change_mpin():
    if request.method == "POST":
        old_pin = request.form.get("old_pin", "").strip()
        new_pin = request.form.get("new_pin", "").strip()
        confirm_pin = request.form.get("confirm_pin", "").strip()
        if current_user.mpin != old_pin:
            flash("Current MPIN is incorrect.", "danger")
        elif len(new_pin) != 6 or not new_pin.isdigit():
            flash("New MPIN must be exactly 6 digits.", "danger")
        elif new_pin != confirm_pin:
            flash("New PINs do not match.", "danger")
        else:
            current_user.mpin = new_pin
            db.session.commit()
            flash("MPIN changed successfully!", "success")
            return redirect(url_for("profile"))
    return render_template("mpin_setup.html", mode="change")


@app.route("/remove-mpin", methods=["POST"])
@login_required
def remove_mpin():
    current_password = request.form.get("current_password", "").strip()
    if current_user.password != current_password:
        flash("Password incorrect. MPIN not removed.", "danger")
    else:
        current_user.mpin = None
        db.session.commit()
        flash("MPIN removed successfully.", "success")
    return redirect(url_for("profile"))


@app.route("/profile")
@login_required
def profile():
    return render_template("profile.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))


@app.route("/tutorial")
@login_required
def tutorial():
    return render_template("tutorial.html")


@app.route("/complete_tutorial")
@login_required
def complete_tutorial():
    current_user.has_seen_tutorial = True
    db.session.commit()
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required
def dashboard():
    # Monthly auto-reset: clear previous month's non-subscription/non-recurring data
    was_reset = run_monthly_reset(current_user)
    if was_reset:
        flash("A new month has started! Your dashboard has been reset. Subscriptions and recurring income are preserved.", "info")

    # Check subscription expiry
    sub_alerts = check_subscription_expiry(current_user.id)
    for alert in sub_alerts:
        if alert["type"] == "expired":
            flash(f"⚠️ Your subscription '{alert['name']}' expired on {alert['end_date'].strftime('%d %b %Y')}. Visit Subscriptions to remove it.", "warning")
        elif alert["type"] == "expiring":
            flash(f"🔔 Your subscription '{alert['name']}' expires in {alert['days_left']} day(s) on {alert['end_date'].strftime('%d %b %Y')}.", "info")

    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)

    # Expenses this month
    expenses = Expense.query.filter(
        Expense.user_id == current_user.id, Expense.date >= month_start
    ).all()
    total_spent = sum(e.amount for e in expenses)
    essential_spent = sum(e.amount for e in expenses if e.is_essential)
    non_essential_spent = total_spent - essential_spent
    needs_pct = (essential_spent / total_spent * 100) if total_spent else 0
    wants_pct = (non_essential_spent / total_spent * 100) if total_spent else 0
    wants_alert = wants_pct > 50

    # Category breakdown
    categories = {}
    for e in expenses:
        categories[e.category] = categories.get(e.category, 0) + e.amount

    # Income this month
    incomes = Income.query.filter(
        Income.user_id == current_user.id, Income.date_received >= month_start
    ).all()
    total_income = sum(i.amount for i in incomes)
    source_breakdown = {}
    for inc in incomes:
        source_breakdown[inc.source] = source_breakdown.get(inc.source, 0) + inc.amount
    source_percentages = {
        src: (amt / total_income * 100) if total_income else 0
        for src, amt in source_breakdown.items()
    }

    # Recent incomes
    recent_incomes = (
        Income.query.filter_by(user_id=current_user.id)
        .order_by(Income.date_received.desc())
        .limit(10)
        .all()
    )
    # Recent expenses
    recent_expenses = (
        Expense.query.filter_by(user_id=current_user.id)
        .order_by(Expense.date.desc())
        .limit(10)
        .all()
    )

    # Burn rate
    days_passed = (now - month_start).days + 1
    burn_rate = total_spent / days_passed if days_passed > 0 else 0

    # Subscriptions (detected)
    subscriptions = detect_subscriptions(current_user.id)
    total_sub_cost = sum(s["avg_amount"] for s in subscriptions)

    # Goals
    goals = (
        Goal.query.filter_by(user_id=current_user.id)
        .order_by(Goal.created_at.desc())
        .all()
    )

    from collections import defaultdict

    monthly_spending = defaultdict(float)

    for exp in expenses:
        month = exp.date.strftime("%b")
        monthly_spending[month] += exp.amount

    essential = 0
    non_essential = 0

    for exp in expenses:
        if exp.is_essential:
            essential += exp.amount
        else:
            non_essential += exp.amount

    total = essential + non_essential

    if total > 0:
        needs_pct = (essential / total) * 100
        wants_pct = (non_essential / total) * 100
    else:
        needs_pct = wants_pct = 0

    wants_alert = wants_pct > 50

    # 50/30/20 Budget Rule
    budget_needs = total_income * 0.50
    budget_wants = total_income * 0.30
    budget_savings = total_income * 0.20

    needs_remaining = budget_needs - essential
    wants_remaining = budget_wants - non_essential
    savings_allocated = budget_savings

    needs_used_pct = min(100, (essential / budget_needs * 100)) if budget_needs > 0 else 0
    wants_used_pct = min(100, (non_essential / budget_wants * 100)) if budget_wants > 0 else 0

    needs_warning = (needs_remaining > 0) and (needs_remaining <= budget_needs * 0.10)
    wants_warning = (wants_remaining > 0) and (wants_remaining <= budget_wants * 0.10)
    needs_over = needs_remaining < 0
    wants_over = wants_remaining < 0

    # Total savings across all goals
    total_savings = sum(g.saved_amount for g in goals)

    # Goal suggestions: recommend where to put savings/extra money based on numeric priority
    def goal_priority_num(g):
        try:
            return int(g.priority)
        except (ValueError, TypeError):
            return 99

    active_goals = sorted(
        [g for g in goals if g.remaining_amount > 0],
        key=goal_priority_num
    )
    goal_suggestions = []
    if total_income > 0 and active_goals:
        # Proportional allocation using inverse-weight: priority 1 gets most
        weights = [1.0 / goal_priority_num(g) for g in active_goals]
        total_weight = sum(weights)
        for g, w in zip(active_goals, weights):
            alloc_pct = w / total_weight if total_weight > 0 else 1.0 / len(active_goals)
            suggested = round(min(budget_savings * alloc_pct, g.remaining_amount), 2)
            goal_suggestions.append({
                "name": g.name,
                "priority": goal_priority_num(g),
                "suggested": suggested,
                "remaining": g.remaining_amount,
                "id": g.id,
            })

    return render_template(
        "dashboard.html",
        now=now,
        total_income=total_income,
        total_spent=total_spent,
        burn_rate=burn_rate,
        source_breakdown=source_breakdown,
        source_percentages=source_percentages,
        essential=essential,
        non_essential=non_essential,
        needs_pct=needs_pct,
        wants_pct=wants_pct,
        wants_alert=wants_alert,
        categories=categories,
        subscriptions=subscriptions,
        total_sub_cost=total_sub_cost,
        recent_incomes=recent_incomes,
        recent_expenses=recent_expenses,
        monthly_spending=dict(monthly_spending),
        budget_needs=budget_needs,
        budget_wants=budget_wants,
        budget_savings=budget_savings,
        needs_remaining=needs_remaining,
        wants_remaining=wants_remaining,
        savings_allocated=savings_allocated,
        needs_used_pct=needs_used_pct,
        wants_used_pct=wants_used_pct,
        needs_warning=needs_warning,
        wants_warning=wants_warning,
        needs_over=needs_over,
        wants_over=wants_over,
        total_savings=total_savings,
        goal_suggestions=goal_suggestions,
        goals=goals,
    )


@app.route("/add_income", methods=["GET", "POST"])
@login_required
def add_income():
    form = IncomeForm()
    if form.validate_on_submit():
        date_received = form.date_received.data
        if date_received:
            from datetime import datetime as dt
            date_received = dt.combine(date_received, dt.min.time())
        else:
            date_received = datetime.utcnow()
        income = Income(
            user_id=current_user.id,
            source=form.source.data,
            amount=form.amount.data,
            date_received=date_received,
            description=form.description.data,
            is_recurring=form.is_recurring.data,
        )
        db.session.add(income)
        db.session.commit()
        flash("Income added successfully!", "success")
        return redirect(url_for("dashboard"))
    incomes = (
        Income.query.filter_by(user_id=current_user.id)
        .order_by(Income.date_received.desc())
        .all()
    )
    return render_template("add_income.html", form=form, edit=False, incomes=incomes)


@app.route("/edit_income/<int:id>", methods=["GET", "POST"])
@login_required
def edit_income(id):
    income = Income.query.get_or_404(id)
    if income.user_id != current_user.id:
        flash("Unauthorized access.", "danger")
        return redirect(url_for("dashboard"))
    form = IncomeForm()
    if form.validate_on_submit():
        income.source = form.source.data
        income.amount = form.amount.data
        income.description = form.description.data
        income.is_recurring = form.is_recurring.data
        if form.date_received.data:
            from datetime import datetime as dt
            income.date_received = dt.combine(form.date_received.data, dt.min.time())
        db.session.commit()
        flash("Income updated.", "success")
        return redirect(url_for("add_income"))
    elif request.method == "GET":
        form.source.data = income.source
        form.amount.data = income.amount
        form.description.data = income.description
        form.is_recurring.data = income.is_recurring
        if income.date_received:
            form.date_received.data = income.date_received.date()
    incomes = (
        Income.query.filter_by(user_id=current_user.id)
        .order_by(Income.date_received.desc())
        .all()
    )
    return render_template("add_income.html", form=form, edit=True, incomes=incomes)


@app.route("/delete_income/<int:id>")
@login_required
def delete_income(id):
    income = Income.query.get_or_404(id)
    if income.user_id != current_user.id:
        flash("Unauthorized access.", "danger")
        return redirect(url_for("dashboard"))
    db.session.delete(income)
    db.session.commit()
    flash("Income deleted.", "success")
    return redirect(url_for("dashboard"))


@app.route("/get_subcategories/<main_category>")
@login_required
def get_subcategories(main_category):
    if main_category in expense_categories:
        return jsonify(
            {
                "subcategories": expense_categories[main_category]["subcategories"],
                "status": "success",
            }
        )
    return jsonify({"subcategories": [], "status": "error"})


@app.route("/add_expense", methods=["GET", "POST"])
@login_required
def add_expense():
    form = ExpenseForm()
    form.main_category.choices = [("", "-- Select Category --")] + [
        (cat, cat) for cat in expense_categories.keys()
    ]

    if request.method == "POST" and form.main_category.data:
        main_cat = form.main_category.data
        if main_cat in expense_categories:
            subcats = expense_categories[main_cat]["subcategories"]
            form.sub_category.choices = [("", "-- Select Sub Category --")] + [
                (sub, sub) for sub in subcats
            ]

    if request.method == "POST":
        if form.validate_on_submit():
            if (
                form.sub_category.data == "Other (User Input)"
                and form.custom_category.data
            ):
                category = form.custom_category.data
            else:
                category = form.sub_category.data

            is_essential = classify_essential(
                form.main_category.data,
                form.sub_category.data,
                form.custom_category.data,
            )

            from datetime import datetime as dt
            exp_date = datetime.utcnow()
            if form.date.data:
                exp_date = dt.combine(form.date.data, dt.min.time())
            sub_start = None
            sub_end = None
            if form.is_subscription.data:
                if form.sub_start_date.data:
                    sub_start = dt.combine(form.sub_start_date.data, dt.min.time())
                if form.sub_end_date.data:
                    sub_end = dt.combine(form.sub_end_date.data, dt.min.time())

            expense = Expense(
                user_id=current_user.id,
                category=category,
                amount=form.amount.data,
                date=exp_date,
                description=form.description.data,
                is_essential=is_essential,
                is_subscription=form.is_subscription.data,
                sub_start_date=sub_start,
                sub_end_date=sub_end,
            )
            db.session.add(expense)
            db.session.commit()
            flash("Expense added successfully!", "success")
            return redirect(url_for("dashboard"))
        else:
            print("Form errors:", form.errors)
            flash("Please check the form and try again.", "danger")
    else:
        form.sub_category.choices = [("", "-- Select Sub Category First --")]

    expenses = (
        Expense.query.filter_by(user_id=current_user.id)
        .order_by(Expense.date.desc())
        .all()
    )
    return render_template("add_expense.html", form=form, edit=False, expenses=expenses)


@app.route("/edit_expense/<int:id>", methods=["GET", "POST"])
@login_required
def edit_expense(id):
    expense = Expense.query.get_or_404(id)
    if expense.user_id != current_user.id:
        flash("Unauthorized access.", "danger")
        return redirect(url_for("dashboard"))

    form = ExpenseForm()
    form.main_category.choices = [("", "-- Select Category --")] + [
        (cat, cat) for cat in expense_categories.keys()
    ]

    if request.method == "POST" and form.main_category.data:
        main_cat = form.main_category.data
        if main_cat in expense_categories:
            subcats = expense_categories[main_cat]["subcategories"]
            form.sub_category.choices = [("", "-- Select Sub Category --")] + [
                (sub, sub) for sub in subcats
            ]

    if form.validate_on_submit():
        if form.sub_category.data == "Other (User Input)" and form.custom_category.data:
            category = form.custom_category.data
        else:
            category = form.sub_category.data

        from datetime import datetime as dt
        expense.category = category
        expense.amount = form.amount.data
        expense.description = form.description.data
        expense.is_subscription = form.is_subscription.data
        expense.is_essential = classify_essential(
            form.main_category.data, form.sub_category.data, form.custom_category.data
        )
        if form.date.data:
            expense.date = dt.combine(form.date.data, dt.min.time())
        if form.is_subscription.data:
            expense.sub_start_date = dt.combine(form.sub_start_date.data, dt.min.time()) if form.sub_start_date.data else None
            expense.sub_end_date = dt.combine(form.sub_end_date.data, dt.min.time()) if form.sub_end_date.data else None
        else:
            expense.sub_start_date = None
            expense.sub_end_date = None

        db.session.commit()
        flash("Expense updated.", "success")
        return redirect(url_for("add_expense"))

    elif request.method == "GET":
        main_cat = None
        for cat, data in expense_categories.items():
            if expense.category in data["subcategories"]:
                main_cat = cat
                break

        if main_cat:
            subcats = expense_categories[main_cat]["subcategories"]
            form.sub_category.choices = [("", "-- Select Sub Category --")] + [
                (sub, sub) for sub in subcats
            ]
            form.main_category.data = main_cat
            form.sub_category.data = expense.category
        else:
            form.sub_category.choices = [
                ("", "-- Select Sub Category --"),
                ("Other (User Input)", "Other (User Input)"),
            ]
            form.main_category.data = "Other"
            form.sub_category.data = "Other (User Input)"
            form.custom_category.data = expense.category

        form.amount.data = expense.amount
        form.description.data = expense.description
        form.is_subscription.data = expense.is_subscription
        if expense.date:
            form.date.data = expense.date.date()
        if expense.sub_start_date:
            form.sub_start_date.data = expense.sub_start_date.date()
        if expense.sub_end_date:
            form.sub_end_date.data = expense.sub_end_date.date()

    expenses = (
        Expense.query.filter_by(user_id=current_user.id)
        .order_by(Expense.date.desc())
        .all()
    )
    return render_template("add_expense.html", form=form, edit=True, expenses=expenses)


@app.route("/delete_expense/<int:id>")
@login_required
def delete_expense(id):
    expense = Expense.query.get_or_404(id)
    if expense.user_id != current_user.id:
        flash("Unauthorized access.", "danger")
        return redirect(url_for("dashboard"))
    db.session.delete(expense)
    db.session.commit()
    flash("Expense deleted.", "success")
    return redirect(url_for("dashboard"))


@app.route("/subscriptions")
@login_required
def subscriptions():
    subs = detect_subscriptions(current_user.id)
    total_cost = sum(s["avg_amount"] for s in subs)
    # Enrich subscriptions with start/end date from the most recent expense
    for sub in subs:
        exps = sub.get("expenses", [])
        if exps:
            latest = max(exps, key=lambda e: e.date)
            sub["sub_start_date"] = latest.sub_start_date
            sub["sub_end_date"] = latest.sub_end_date
            sub["expense_id"] = latest.id
        else:
            sub["sub_start_date"] = None
            sub["sub_end_date"] = None
            sub["expense_id"] = None
    now = datetime.utcnow()
    return render_template(
        "subscriptions.html", subscriptions=subs, total_cost=total_cost, now=now
    )


@app.route("/goals")
@login_required
def goals():
    goals = (
        Goal.query.filter_by(user_id=current_user.id)
        .order_by(Goal.created_at.desc())
        .all()
    )
    return render_template("goals.html", goals=goals)


@app.route("/add_goal", methods=["GET", "POST"])
@login_required
def add_goal():
    form = GoalForm()
    if form.validate_on_submit():
        goal = Goal(
            user_id=current_user.id,
            name=form.name.data,
            target_amount=form.target_amount.data,
            monthly_savings=form.monthly_savings.data,
            target_date=form.target_date.data,
            priority=form.priority.data,
        )
        db.session.add(goal)
        db.session.commit()
        flash("Goal created successfully!", "success")
        return redirect(url_for("goals"))
    goals = (
        Goal.query.filter_by(user_id=current_user.id)
        .order_by(Goal.created_at.desc())
        .all()
    )
    return render_template("add_goal.html", form=form, goals=goals)


@app.route("/goal/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit_goal(id):
    goal = Goal.query.get_or_404(id)
    if goal.user_id != current_user.id:
        flash("Unauthorized access.", "danger")
        return redirect(url_for("goals"))
    form = GoalForm()
    if form.validate_on_submit():
        goal.name = form.name.data
        goal.target_amount = form.target_amount.data
        goal.monthly_savings = form.monthly_savings.data
        goal.target_date = form.target_date.data
        goal.priority = form.priority.data
        db.session.commit()
        flash("Goal updated successfully!", "success")
        return redirect(url_for("goal_detail", id=goal.id))
    elif request.method == "GET":
        form.name.data = goal.name
        form.target_amount.data = goal.target_amount
        form.monthly_savings.data = goal.monthly_savings
        form.target_date.data = goal.target_date
        form.priority.data = str(goal.priority)
    return render_template("edit_goal.html", form=form, goal=goal)


@app.route("/goal/<int:id>", methods=["GET", "POST"])
@login_required
def goal_detail(id):
    goal = Goal.query.get_or_404(id)
    if goal.user_id != current_user.id:
        flash("Unauthorized access.", "danger")
        return redirect(url_for("goals"))

    form = SavingsUpdateForm()
    if form.validate_on_submit():
        # Add the new amount to existing saved amount instead of replacing
        additional_savings = form.saved_amount.data
        goal.saved_amount = goal.saved_amount + additional_savings
        db.session.commit()
        flash(
            f"Added ₹{additional_savings:,.0f} to your savings! Total saved: ₹{goal.saved_amount:,.0f}",
            "success",
        )
        return redirect(url_for("goal_detail", id=id))

    # Get spending reduction suggestions
    suggestions = get_spending_suggestions(current_user.id, goal)

    # Calculate timeline milestones
    milestones = []
    if goal.monthly_savings > 0:
        for month in range(1, min(13, int(goal.estimated_months) + 1)):
            milestone_date = datetime.utcnow() + timedelta(days=30 * month)
            milestone_amount = goal.saved_amount + (goal.monthly_savings * month)
            milestones.append(
                {
                    "month": month,
                    "date": milestone_date,
                    "amount": min(milestone_amount, goal.target_amount),
                }
            )

    return render_template(
        "goal_detail.html",
        goal=goal,
        form=form,
        suggestions=suggestions,
        milestones=milestones,
    )


@app.route("/goal/<int:id>/delete")
@login_required
def delete_goal(id):
    goal = Goal.query.get_or_404(id)
    if goal.user_id != current_user.id:
        flash("Unauthorized access.", "danger")
        return redirect(url_for("goals"))

    db.session.delete(goal)
    db.session.commit()
    flash("Goal deleted.", "success")
    return redirect(url_for("goals"))


@app.route("/what_if/<int:id>", methods=["POST"])
@login_required
def what_if(id):
    goal = Goal.query.get_or_404(id)
    if goal.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json()
    new_monthly_savings = data.get("monthly_savings", goal.monthly_savings)
    spending_reduction = data.get("spending_reduction", 0)

    total_monthly = new_monthly_savings + spending_reduction
    if total_monthly <= 0:
        return jsonify(
            {"months": float("inf"), "date": None, "progress": goal.progress_percentage}
        )

    remaining = goal.remaining_amount
    months = remaining / total_monthly

    from datetime import timedelta

    estimated_date = datetime.utcnow() + timedelta(days=30 * months)

    return jsonify(
        {
            "months": round(months, 1),
            "date": estimated_date.strftime("%d %b %Y"),
            "progress": goal.progress_percentage,
        }
    )


def make_chart(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=110, transparent=True)
    buf.seek(0)
    img_b64 = base64.b64encode(buf.read()).decode('utf-8')
    plt.close(fig)
    return img_b64


def chart_expense_distribution(categories):
    if not categories:
        return None
    labels = list(categories.keys())
    values = list(categories.values())
    colors = ['#6C5CE7','#00CEC9','#FD79A8','#FDCB6E','#55EFC4',
              '#E17055','#0984E3','#A29BFE','#00B894','#74B9FF']
    fig, ax = plt.subplots(figsize=(5, 4))
    fig.patch.set_alpha(0)
    wedges, texts, autotexts = ax.pie(
        values, labels=None, autopct='%1.0f%%',
        colors=colors[:len(values)], startangle=140,
        wedgeprops=dict(width=0.6, edgecolor='white', linewidth=2),
        pctdistance=0.78
    )
    for t in autotexts:
        t.set_fontsize(9)
        t.set_color('white')
        t.set_fontweight('bold')
    ax.legend(wedges, [f'{l} (₹{v:,.0f})' for l, v in zip(labels, values)],
              loc='lower center', bbox_to_anchor=(0.5, -0.22),
              ncol=2, fontsize=8, frameon=False)
    ax.set_title('Expense Distribution', fontsize=12, fontweight='bold', pad=10, color='#2d3436')
    return make_chart(fig)


def chart_income_vs_expense(monthly_inc, monthly_exp):
    months = sorted(set(list(monthly_inc.keys()) + list(monthly_exp.keys())))
    if not months:
        return None
    inc_vals = [monthly_inc.get(m, 0) for m in months]
    exp_vals = [monthly_exp.get(m, 0) for m in months]
    x = range(len(months))
    fig, ax = plt.subplots(figsize=(7, 3.8))
    fig.patch.set_alpha(0)
    w = 0.35
    bars1 = ax.bar([i - w/2 for i in x], inc_vals, width=w, color='#00b894', label='Income',
                   edgecolor='white', linewidth=1.2, zorder=3)
    bars2 = ax.bar([i + w/2 for i in x], exp_vals, width=w, color='#e17055', label='Expense',
                   edgecolor='white', linewidth=1.2, zorder=3)
    for bar in list(bars1) + list(bars2):
        if bar.get_height() > 0:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 100,
                    f'₹{bar.get_height():,.0f}', ha='center', va='bottom',
                    fontsize=7.5, color='#636e72')
    ax.set_xticks(list(x))
    ax.set_xticklabels(months, fontsize=9)
    ax.tick_params(axis='y', labelsize=8)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'₹{v/1000:.0f}k' if v >= 1000 else f'₹{v:.0f}'))
    ax.set_axisbelow(True)
    ax.yaxis.grid(True, color='#dfe6e9', linewidth=0.8)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#dfe6e9')
    ax.spines['bottom'].set_color('#dfe6e9')
    ax.legend(fontsize=9, frameon=False)
    ax.set_title('Income vs Expense', fontsize=12, fontweight='bold', pad=10, color='#2d3436')
    fig.tight_layout()
    return make_chart(fig)


def chart_monthly_trend(monthly_spending):
    if not monthly_spending:
        return None
    months = list(monthly_spending.keys())
    values = list(monthly_spending.values())
    fig, ax = plt.subplots(figsize=(7, 3.8))
    fig.patch.set_alpha(0)
    ax.fill_between(months, values, alpha=0.12, color='#6C5CE7', zorder=1)
    ax.plot(months, values, color='#6C5CE7', linewidth=2.5, marker='o',
            markersize=7, markerfacecolor='white', markeredgecolor='#6C5CE7',
            markeredgewidth=2, zorder=2)
    for i, (m, v) in enumerate(zip(months, values)):
        ax.text(i, v + max(values) * 0.03, f'₹{v:,.0f}',
                ha='center', va='bottom', fontsize=8, color='#6C5CE7', fontweight='bold')
    ax.set_xticks(range(len(months)))
    ax.set_xticklabels(months, fontsize=9)
    ax.tick_params(axis='y', labelsize=8)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'₹{v/1000:.0f}k' if v >= 1000 else f'₹{v:.0f}'))
    ax.set_axisbelow(True)
    ax.yaxis.grid(True, color='#dfe6e9', linewidth=0.8)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#dfe6e9')
    ax.spines['bottom'].set_color('#dfe6e9')
    ax.set_title('Monthly Spending Trend', fontsize=12, fontweight='bold', pad=10, color='#2d3436')
    fig.tight_layout()
    return make_chart(fig)


def chart_category_breakdown(categories):
    if not categories:
        return None
    sorted_cats = sorted(categories.items(), key=lambda x: x[1], reverse=True)[:8]
    labels = [c[0] for c in sorted_cats]
    values = [c[1] for c in sorted_cats]
    colors = ['#6C5CE7','#0984E3','#00CEC9','#00b894','#55EFC4',
              '#FDCB6E','#E17055','#FD79A8']
    fig, ax = plt.subplots(figsize=(6, max(3.5, len(labels) * 0.5)))
    fig.patch.set_alpha(0)
    bars = ax.barh(labels[::-1], values[::-1], color=colors[:len(values)],
                   edgecolor='white', linewidth=1, height=0.6, zorder=3)
    for bar, val in zip(bars, values[::-1]):
        ax.text(bar.get_width() + max(values) * 0.01, bar.get_y() + bar.get_height()/2,
                f'₹{val:,.0f}', va='center', fontsize=8.5, color='#636e72')
    ax.set_axisbelow(True)
    ax.xaxis.grid(True, color='#dfe6e9', linewidth=0.8)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#dfe6e9')
    ax.spines['bottom'].set_color('#dfe6e9')
    ax.tick_params(axis='y', labelsize=9)
    ax.tick_params(axis='x', labelsize=8)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'₹{v/1000:.0f}k' if v >= 1000 else f'₹{v:.0f}'))
    ax.set_title('Category Breakdown', fontsize=12, fontweight='bold', pad=10, color='#2d3436')
    fig.tight_layout()
    return make_chart(fig)


@app.route("/analysis")
@login_required
def analysis():
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    six_months_ago = now - timedelta(days=180)

    expenses_month = Expense.query.filter(
        Expense.user_id == current_user.id,
        Expense.date >= month_start
    ).all()

    all_expenses = Expense.query.filter(
        Expense.user_id == current_user.id,
        Expense.date >= six_months_ago
    ).all()

    all_incomes = Income.query.filter(
        Income.user_id == current_user.id,
        Income.date_received >= six_months_ago
    ).all()

    categories = {}
    for e in expenses_month:
        categories[e.category] = categories.get(e.category, 0) + e.amount

    monthly_spending = defaultdict(float)
    for exp in all_expenses:
        key = exp.date.strftime('%b %Y')
        monthly_spending[key] += exp.amount
    sorted_months = sorted(monthly_spending.keys(),
                           key=lambda m: datetime.strptime(m, '%b %Y'))
    monthly_spending_ordered = {m: monthly_spending[m] for m in sorted_months}

    monthly_income = defaultdict(float)
    for inc in all_incomes:
        key = inc.date_received.strftime('%b %Y')
        monthly_income[key] += inc.amount

    monthly_expense_all = defaultdict(float)
    for exp in all_expenses:
        key = exp.date.strftime('%b %Y')
        monthly_expense_all[key] += exp.amount

    chart_dist  = chart_expense_distribution(categories)
    chart_inc_exp = chart_income_vs_expense(dict(monthly_income), dict(monthly_expense_all))
    chart_trend = chart_monthly_trend(monthly_spending_ordered)
    chart_cats  = chart_category_breakdown(categories)

    total_income = sum(i.amount for i in Income.query.filter(
        Income.user_id == current_user.id,
        Income.date_received >= month_start
    ).all())
    total_spent = sum(e.amount for e in expenses_month)
    essential = sum(e.amount for e in expenses_month if e.is_essential)
    non_essential = total_spent - essential

    days_passed = (now - month_start).days + 1
    burn_rate = total_spent / days_passed if days_passed > 0 else 0

    return render_template(
        "analysis.html",
        chart_dist=chart_dist,
        chart_inc_exp=chart_inc_exp,
        chart_trend=chart_trend,
        chart_cats=chart_cats,
        categories=categories,
        total_income=total_income,
        total_spent=total_spent,
        essential=essential,
        non_essential=non_essential,
        burn_rate=burn_rate,
        now=now,
    )


# ─────────────────────────────────────────────────────────────
#  Email Import Routes
# ─────────────────────────────────────────────────────────────

@app.route("/email-import")
@login_required
def email_import():
    imap_connected = "imap_config" in session
    return render_template("email_import.html", imap_connected=imap_connected,
                           presets=IMAP_PRESETS)


@app.route("/email-import/connect", methods=["POST"])
@login_required
def email_import_connect():
    data = request.get_json(force=True)
    host     = data.get("host", "").strip()
    port     = int(data.get("port", 993))
    email_addr = data.get("email", "").strip()
    password = data.get("password", "").strip()

    if not all([host, email_addr, password]):
        return jsonify({"success": False, "message": "All fields are required."})

    try:
        ctx = ssl.create_default_context()
        mail = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
        mail.login(email_addr, password)
        mail.logout()
        session["imap_config"] = {
            "host": host, "port": port,
            "email": email_addr, "password": password,
        }
        return jsonify({"success": True, "message": f"Connected to {host} successfully!"})
    except imaplib.IMAP4.error as e:
        return jsonify({"success": False, "message": f"Authentication failed — check your email/password or App Password. ({e})"})
    except Exception as e:
        return jsonify({"success": False, "message": f"Connection failed: {e}"})


@app.route("/email-import/disconnect", methods=["POST"])
@login_required
def email_import_disconnect():
    session.pop("imap_config", None)
    return jsonify({"success": True})


@app.route("/email-import/scan", methods=["POST"])
@login_required
def email_import_scan():
    cfg = session.get("imap_config")
    if not cfg:
        return jsonify({"success": False, "message": "Not connected to any email account."})
    days = int(request.get_json(force=True).get("days", 30))
    try:
        transactions = scan_imap_emails(cfg["host"], cfg["port"], cfg["email"], cfg["password"], days=days)
        return jsonify({"success": True, "transactions": transactions, "count": len(transactions)})
    except imaplib.IMAP4.error as e:
        session.pop("imap_config", None)
        return jsonify({"success": False, "message": f"Email session expired — please reconnect. ({e})"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/email-import/import", methods=["POST"])
@login_required
def email_import_do():
    items = request.get_json(force=True).get("transactions", [])
    imported_count = 0
    for txn in items:
        try:
            txn_date = datetime.strptime(txn["date"], "%Y-%m-%d")
            amount   = float(txn["amount"])
            desc     = txn.get("description", "")[:200]
            txn_type = txn.get("type", "expense")
            sub_cat  = txn.get("sub_cat", "Miscellaneous")
            main_cat = txn.get("main_cat", "Other Expenses")

            if txn_type == "income":
                source = txn.get("sub_cat", "Email Import")
                inc = Income(
                    user_id=current_user.id,
                    source=source,
                    amount=amount,
                    date_received=txn_date,
                    description=desc,
                )
                db.session.add(inc)
            else:
                exp = Expense(
                    user_id=current_user.id,
                    category="Uncategorized",
                    amount=amount,
                    date=txn_date,
                    description=desc,
                    is_essential=False,
                    is_subscription=False,
                )
                db.session.add(exp)
            imported_count += 1
        except Exception:
            continue
    db.session.commit()
    return jsonify({"success": True, "imported": imported_count})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
