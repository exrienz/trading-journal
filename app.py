import os
from dotenv import load_dotenv
from datetime import datetime, date
from functools import wraps
import hashlib
import logging

import requests
import jwt
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import case, func
from urllib.parse import quote_plus

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# ─── Load environment variables ────────────────────────────────────────────────
load_dotenv()

# ─── Configuration ─────────────────────────────────────────────────────────────
DB_USER     = os.getenv('DB_USER')
DB_PASS     = os.getenv('DB_PASS')
DB_PASS_ENC = quote_plus(DB_PASS)
DB_HOST     = os.getenv('DB_HOST')
DB_NAME     = os.getenv('DB_NAME')

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI']      = (
    f"mysql+pymysql://{DB_USER}:{DB_PASS_ENC}@{DB_HOST}/{DB_NAME}"
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JWT_SECRET']                    = os.getenv('JWT_SECRET')
app.config['GEMINI_API_KEY']                = os.getenv('GEMINI_API_KEY')

# ─── Initialize database ───────────────────────────────────────────────────────
db = SQLAlchemy(app)

# Create all database tables
with app.app_context():
    db.create_all()

# ─── Models ─────────────────────────────────────────────────────────────────────
class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)

    def set_password(self, password):
        self.password_hash = hashlib.sha256(password.encode()).hexdigest()

    def check_password(self, password):
        return self.password_hash == hashlib.sha256(password.encode()).hexdigest()

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
        parts = request.headers.get('Authorization','').split()
        if len(parts)==2 and parts[0]=='Bearer':
            try:
                data = jwt.decode(parts[1], app.config['JWT_SECRET'], algorithms=['HS256'])
                request.user = User.query.get(data['user_id'])
            except jwt.InvalidTokenError:
                return jsonify({'error':'Invalid token'}), 401
            return f(*args, **kwargs)
        return jsonify({'error':'Missing/invalid Authorization header'}), 401
    return wrapper

# ─── Gemini AI Integration ────────────────────────────────────────────────────
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)
def call_gemini(prompt_text):
    if not app.config['GEMINI_API_KEY']:
        return "Gemini API key not configured"
    try:
        payload = {"contents":[{"parts":[{"text": prompt_text}]}]}
        params  = {'key': app.config['GEMINI_API_KEY']}
        r       = requests.post(GEMINI_URL, params=params, json=payload)
        r.raise_for_status()
        return r.json()['candidates'][0]['content']
    except Exception as e:
        return f"Error calling Gemini API: {str(e)}"

# ─── Routes ────────────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    try:
        # Check if tables exist
        tables = db.engine.table_names()
        logger.debug(f"Database tables: {tables}")
        
        # Try to query the User table
        user_count = User.query.count()
        logger.debug(f"Number of users in database: {user_count}")
        
        return jsonify({
            'status': 'ok',
            'database': {
                'tables': tables,
                'user_count': user_count
            }
        }), 200
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500

@app.route('/')
def index():
    return redirect(url_for('dashboard'))

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method=='POST':
        try:
            username = request.form['username']
            password = request.form['password']
            logger.debug(f"Attempting to register user: {username}")
            
            # Check if user already exists
            existing_user = User.query.filter_by(username=username).first()
            if existing_user:
                logger.debug(f"User {username} already exists")
                return jsonify({'error': 'Username already exists'}), 400
            
            user = User(username=username)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            logger.debug(f"Successfully registered user: {username}")
            return redirect(url_for('login'))
        except Exception as e:
            logger.error(f"Error during registration: {str(e)}")
            db.session.rollback()
            return jsonify({'error': str(e)}), 500
    return render_template('register.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        try:
            username = request.form['username']
            password = request.form['password']
            logger.debug(f"Attempting login for user: {username}")
            
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                token = generate_token(user.id)
                logger.debug(f"Successful login for user: {username}")
                return jsonify({'token': token})
            logger.debug(f"Failed login attempt for user: {username}")
            return jsonify({'error': 'Bad credentials'}), 401
        except Exception as e:
            logger.error(f"Error during login: {str(e)}")
            return jsonify({'error': str(e)}), 500
    return render_template('login.html')

@app.route('/deposit', methods=['POST'])
@auth_required
def deposit():
    amt = float(request.form['amount'])
    tx  = Transaction(user_id=request.user.id, type='deposit', amount=amt)
    db.session.add(tx)
    db.session.commit()
    return jsonify({'status':'ok'})

@app.route('/withdraw', methods=['POST'])
@auth_required
def withdraw():
    amt = float(request.form['amount'])
    tx  = Transaction(user_id=request.user.id, type='withdraw', amount=amt)
    db.session.add(tx)
    db.session.commit()
    return jsonify({'status':'ok'})

@app.route('/daily/<day>', methods=['GET','POST'])
@auth_required
def daily(day):
    d   = date.fromisoformat(day)
    rec = DailyTrade.query.filter_by(user_id=request.user.id, trade_date=d).first()
    if request.method=='POST':
        if not rec:
            rec = DailyTrade(user_id=request.user.id, trade_date=d)
        rec.profit        = request.form['profit']
        rec.loss          = request.form['loss']
        rec.reason_profit = request.form['reason_profit']
        rec.reason_loss   = request.form['reason_loss']
        db.session.add(rec)
        db.session.commit()
        return redirect(url_for('dashboard'))
    return render_template('daily.html', rec=rec, day=d)

@app.route('/dashboard')
@auth_required
def dashboard():
    user = request.user

    # Sum deposits & withdrawals
    dep_amt, wd_amt = db.session.query(
        func.sum(case([(Transaction.type=='deposit', Transaction.amount)], else_=0)),
        func.sum(case([(Transaction.type=='withdraw', Transaction.amount)], else_=0))
    ).filter(Transaction.user_id==user.id).one()

    # Sum profits & losses
    prof_sum = db.session.query(func.sum(DailyTrade.profit)) \
                .filter(DailyTrade.user_id==user.id).scalar() or 0
    loss_sum = db.session.query(func.sum(DailyTrade.loss)) \
                .filter(DailyTrade.user_id==user.id).scalar() or 0

    # Active Balance = ∑ deposits − ∑ withdrawals + ∑ profits − ∑ losses
    active_balance = (dep_amt or 0) - (wd_amt or 0) + prof_sum - loss_sum
    total_pl       = prof_sum - loss_sum

    # Collect reasons
    profits = [
        r.reason_profit
        for r in DailyTrade.query.filter_by(user_id=user.id).all()
        if r.reason_profit
    ]
    losses  = [
        r.reason_loss
        for r in DailyTrade.query.filter_by(user_id=user.id).all()
        if r.reason_loss
    ]

    # Single-line Gemini calls
    tips    = call_gemini("Generate trading tips from these profit reasons:\n" + "\n".join(profits))
    lessons = call_gemini("Generate trading lessons from these loss reasons:\n"  + "\n".join(losses))

    return render_template(
        'dashboard.html',
        active_balance=active_balance,
        deposit_amt=dep_amt,
        withdraw_amt=wd_amt,
        total_pl=total_pl,
        tips=tips,
        lessons=lessons
    )

# ─── Run the app ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)
