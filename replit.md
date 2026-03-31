# Money Mate - Personal Finance Manager

## Project Overview
A Flask-based personal finance web app that helps users track income, expenses, and savings goals. It provides spending insights, subscription detection, and budget analysis using the 50/30/20 rule.

## Architecture
- **Backend**: Python / Flask
- **Database**: SQLite via SQLAlchemy (`finance.db`)
- **Auth**: Flask-Login (session-based)
- **Forms**: Flask-WTF / WTForms
- **Admin Panel**: Flask-Admin (accessible at `/admin` for admin user)
- **Frontend**: Jinja2 templates, Bootstrap 5, Chart.js

## Directory Structure
```
├── app.py              # Main Flask application, routes, and business logic
├── models.py           # SQLAlchemy database models (User, Income, Expense, Goal)
├── forms.py            # WTForms form definitions
├── requirements.txt    # Python dependencies
├── finance.db          # SQLite database (auto-created on first run)
├── templates/          # Jinja2 HTML templates
│   ├── base.html
│   ├── index.html
│   ├── dashboard.html
│   ├── login.html
│   ├── register.html
│   ├── tutorial.html
│   ├── add_income.html
│   ├── add_expense.html
│   ├── add_goal.html
│   ├── edit_goal.html
│   ├── goal_detail.html
│   ├── goals.html
│   ├── subscriptions.html
│   ├── analysis.html
│   ├── email_import.html
│   └── profile.html
└── static/
    └── style.css       # Custom styles
```

## Key Features
- **Dashboard**: Monthly overview with burn rate, 50/30/20 budget tracking
- **Expense Tracking**: Category/subcategory system with needs vs wants classification
- **Subscription Detection**: Auto-detects recurring expenses
- **Goal Tracking**: Savings goals with milestones and what-if scenarios
- **Analysis Page**: Charts and spending breakdowns
- **Email Import (IMAP)**: Connects to user's inbox via IMAP/SSL, scans for financial emails, parses transaction amounts/categories, and imports them as expenses or income. Credentials are session-only (never stored in DB). Routes: `/email-import`, `/email-import/connect`, `/email-import/scan`, `/email-import/import`, `/email-import/disconnect`.
- **Admin Panel**: `/admin` route (requires username "admin")
- **Dark Mode**: Toggle via navbar button (persisted in localStorage)

## Environment Variables
- `SESSION_SECRET`: Flask secret key for sessions (required)
- `ADMIN_PASSWORD`: Password for the `/create_admin` route (optional)

## Running the App
- Development: `python app.py` (runs on port 5000)
- Production: `gunicorn --bind=0.0.0.0:5000 --reuse-port app:app`

## Notes
- Passwords are stored in plaintext in the current implementation (no hashing)
- The database is auto-created/migrated on startup via `db.create_all()` and ALTER TABLE statements
- Admin user is created via the `/create_admin` route
