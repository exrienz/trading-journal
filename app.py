import os
from dotenv import load_dotenv
from datetime import datetime, date
from functools import wraps
import requests
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
import jwt
from sqlalchemy import case, func
from urllib.parse import quote_plus

# ─── Load environment variables ────────────────────────────────────────────────
load_dotenv()

# ─── Configuration ─────────────────────────────────────────────────────────────
DB_USER    = os.getenv('DB_USER')
DB_PASS    = os.getenv('DB_PASS')
DB_PASS_ENC = quote_plus(DB_PASS)
DB_HOST    = os.getenv('DB_HOST')
DB_NAME    = os.getenv('DB_NAME')

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = (
    f"mysql+pymysql://{DB_USER}:{DB_PASS_ENC}@{DB_HOST}/{DB_NAME}"
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JWT_SECRET']      = os.getenv('JWT_SECRET')
app.config['GEMINI_API_KEY']  = os.getenv('GEMINI_API_KEY')

# ─── Initialize database ───────────────────────────────────────────────────────
db = SQLAlchemy(app)

# ─── Models ─────────────────────────────────────────────────────────────────────
class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)

class Transaction(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    user_id   = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    type      = db.Column(db.Enum('deposit','withdraw'), nullable=False)
    amount    = db.Column(db.Numeric(12,2), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class DailyTrade(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    trade_date    = db.Column(db.Date, nullable=False)
    profit        = db.Column(db.Numeric(12,2), default=0)
    loss          = db.Column(db.Numeric(12,2), default=0)
    reason_profit = db.Column(db.Text, default='')
    reason_loss   = db.Column(db.Text, default='')
    __table_args__ = (
        db.UniqueConstraint('user_id','trade_date', name='uix_user_date'),
    )

# ─── JWT Helpers ───────────────────────────────────────────────────────────────
def generate_token(user_id):
    payload = {'user_id': user_id, 'iat': datetime.utcnow()}
    return jwt.encode(payload, app.config['JWT_SECRET'], algorithm='HS256')

def auth_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        parts = request.headers.get('Authorization', '').split()
        if len(parts) == 2 and parts[0] == 'Bearer':
            try:
                data = jwt.decode(parts[1], app.config['JWT_SECRET'], algorithms=['HS256'])
                request.user = User.query.get(data['user_id'])
            except jwt.InvalidTokenError:
                return jsonify({'error':'Invalid token'}), 401
            return f(*args, **kwargs)
        return jsonify({'error':'Missing or invalid Authorization header'}), 401
    return wrapper

# ─── Gemini AI Integration ────────────────────────────────────────────────────
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)
def call_gemini(prompt_text):
    payload = {"contents":[{"parts":[{"text": prompt_text}]}]}
    params  = {'key': app.config['GEMINI_API_KEY']}
    resp    = requests.post(GEMINI_URL, params=params, json=payload)
    resp.raise_for_status()
    return resp.json()['candidates'][0]['content']

# ─── Health & Index ───────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status':'ok'}), 200

@app.route('/')
def index():
    return redirect(url_for('dashboard'))

# ─── Auth Routes ──────────────────────────────────────────────────────────────
@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        user = User(
            username=request.form['username'],
            password_hash=request.form['password']
        )
        db.session.add(user)
        db.session.commit()
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and user.password_hash == request.form['password']:
            token = generate_token(user.id)
            return jsonify({'token': token})
        return jsonify({'error':'Bad credentials'}), 401
    return render_template('login.html')

# ─── Transaction Routes ───────────────────────────────────────────────────────
@app.route('/deposit', methods=['POST'])
@auth_required
def deposit():
    amt = float(request.form['amount'])
    tx  = Transaction(user_id=request.user.id, type='deposit', amount=amt)
    db.session.add(tx); db.session.commit()
    return jsonify({'status':'ok'})

@app.route('/withdraw', methods=['POST'])
@auth_required
def withdraw():
    amt = float(request.form['amount'])
    tx  = Transaction(user_id=request.user.id, type='withdraw', amount=amt)
    db.session.add(tx); db.session.commit()
    return jsonify({'status':'ok'})

# ─── Daily Trade CRUD ─────────────────────────────────────────────────────────
@app.route('/daily/<day>', methods=['GET','POST'])
@auth_required
def daily(day):
    d   = date.fromisoformat(day)
    rec = DailyTrade.query.filter_by(user_id=request.user.id, trade_date=d).first()
    if request.method == 'POST':
        if not rec:
            rec = DailyTrade(user_id=request.user.id, trade_date=d)
        rec.profit        = request.form['profit']
        rec.loss          = request.form['loss']
        rec.reason_profit = request.form['reason_profit']
        rec.reason_loss   = request.form['reason_loss']
        db.session.add(rec); db.session.commit()
        return redirect(url_for('dashboard'))
    return render_template('daily.html', rec=rec, day=d)

# ─── Dashboard ────────────────────────────────────────────────────────────────
@app.route('/dashboard')
@auth_required
def dashboard():
    user = request.user

    # Sum deposits & withdrawals
    deposit_amt, withdraw_amt = (
        db.session.query(
            func.sum(case([(Transaction.type=='deposit', Transaction.amount)], else_=0)),
            func.sum(case([(Transaction.type=='withdraw', Transaction.amount)], else_=0))
        )
        .filter(Transaction.user_id == user.id)
        .one()
    )

    # Sum profits & losses
    profits_sum = db.session.query(func.sum(DailyTrade.profit))\
        .filter(DailyTrade.user_id == user.id).scalar() or 0
    losses_sum  = db.session.query(func.sum(DailyTrade.loss))\
        .filter(DailyTrade.user_id == user.id).scalar() or 0

    # Active Balance formula:
    #   ∑ deposits − ∑ withdrawals + ∑ profits − ∑ losses
    active_balance = (deposit_amt or 0) \
                     - (withdraw_amt or 0) \
                     + profits_sum \
                     - losses_sum
    total_pl = profits_sum - losses_sum

    # Gather reasons for AI
    profits = [r.reason_profit for r in DailyTrade.query.filter_by(user_id=user.id).all() if r.reason_profit]
    losses  = [r.reason_loss   for r in DailyTrade.query.filter_by(user_id=user.id).all() if r.reason_loss]

    # Single-line AI calls
    tips    = call_gemini("Generate trading tips from these profit reasons:\n" + "\n".join(profits))
    lessons = call_gemini("Generate trading lessons from these loss reasons:\n"  + "\n".join(losses))

    return render_template(
        'dashboard.html',
        active_balance=active_balance,
        deposit_amt=deposit_amt,
        withdraw_amt=withdraw_amt,
        total_pl=total_pl,
        tips=tips,
        lessons=lessons
    )

# ─── Startup ───────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    db.create_all()
    app.run(host='0.0.0.0', debug=True)

