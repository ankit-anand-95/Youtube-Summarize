import os
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt

db = SQLAlchemy()
bcrypt = Bcrypt()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class User(db.Model):
    __tablename__ = "users"
    id                = db.Column(db.Integer, primary_key=True)
    email             = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash     = db.Column(db.String(255), nullable=False)
    daily_count       = db.Column(db.Integer, default=0)
    count_date        = db.Column(db.String(20), default="")
    created_at        = db.Column(db.String(50), default=lambda: datetime.now().isoformat())
    summaries         = db.relationship("Summary", back_populates="user", lazy="dynamic")

    # Flask-Login interface
    @property
    def is_authenticated(self): return True
    @property
    def is_active(self): return True
    @property
    def is_anonymous(self): return False
    def get_id(self): return str(self.id)

    def set_password(self, password: str):
        self.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")

    def check_password(self, password: str) -> bool:
        return bcrypt.check_password_hash(self.password_hash, password)



class Summary(db.Model):
    __tablename__ = "summaries"
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    video_id   = db.Column(db.String(50), index=True)
    url        = db.Column(db.Text)
    title      = db.Column(db.Text)
    channel    = db.Column(db.Text)
    date       = db.Column(db.String(50))
    lang       = db.Column(db.String(10))
    summary    = db.Column(db.Text)
    created_at = db.Column(db.String(50), default=lambda: datetime.now().isoformat())
    user       = db.relationship("User", back_populates="summaries")


def init_db(app):
    db.init_app(app)
    bcrypt.init_app(app)
    with app.app_context():
        db.create_all()


def get_user_by_email(email: str):
    return User.query.filter_by(email=email.lower().strip()).first()

def get_user_by_id(user_id: int):
    return db.session.get(User, user_id)

def create_user(email: str, password: str):
    u = User(email=email.lower().strip())
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    return u


def _row(s):
    return {"id": s.id, "user_id": s.user_id, "video_id": s.video_id, "url": s.url,
        "title": s.title, "channel": s.channel, "date": s.date,
        "lang": s.lang or "en", "summary": s.summary, "created_at": s.created_at}

def save_summary(data: dict, user_id=None) -> int:
    s = Summary(user_id=user_id, video_id=data["video_id"], url=data["url"],
        title=data["title"], channel=data["channel"], date=data["date"],
        lang=data.get("lang", "en"), summary=data["summary"],
        created_at=data.get("created_at", datetime.now().isoformat()))
    db.session.add(s)
    db.session.commit()
    return s.id

def get_all_summaries(user_id=None):
    q = Summary.query
    if user_id:
        q = q.filter_by(user_id=user_id)
    return [_row(s) for s in q.order_by(Summary.id.desc()).all()]

def get_summary(summary_id: int, user_id=None):
    s = db.session.get(Summary, summary_id)
    if not s:
        return None
    if user_id and s.user_id and s.user_id != user_id:
        return None
    return _row(s)

def delete_summary(summary_id: int, user_id=None):
    s = db.session.get(Summary, summary_id)
    if s and (not user_id or s.user_id == user_id):
        db.session.delete(s)
        db.session.commit()

def delete_all_summaries(user_id=None):
    q = Summary.query
    if user_id:
        q = q.filter_by(user_id=user_id)
    q.delete()
    db.session.commit()

def get_summary_by_video_id(video_id: str, user_id=None):
    q = Summary.query.filter_by(video_id=video_id)
    if user_id:
        q = q.filter_by(user_id=user_id)
    s = q.order_by(Summary.id.desc()).first()
    return _row(s) if s else None

def check_and_increment_usage(user, daily_limit: int) -> tuple[bool, int]:
    """Returns (allowed, remaining_after). Resets count daily."""
    today = datetime.now().strftime("%Y-%m-%d")
    if user.count_date != today:
        user.daily_count = 0
        user.count_date = today
    if user.daily_count >= daily_limit:
        remaining = 0
        db.session.commit()
        return False, 0
    user.daily_count += 1
    db.session.commit()
    return True, max(0, daily_limit - user.daily_count)

def get_usage_today(user, daily_limit: int) -> dict:
    """Returns current usage info without incrementing."""
    today = datetime.now().strftime("%Y-%m-%d")
    count = user.daily_count if user.count_date == today else 0
    return {"used": count, "limit": daily_limit, "remaining": max(0, daily_limit - count)}


def search_summaries(query: str, user_id=None):
    from sqlalchemy import case
    pattern = f"%{query}%"
    q = Summary.query.filter(
        Summary.title.ilike(pattern) |
        Summary.channel.ilike(pattern) |
        Summary.summary.ilike(pattern))
    if user_id:
        q = q.filter_by(user_id=user_id)
    q = q.order_by(
        case((Summary.title.ilike(pattern), 0),
             (Summary.channel.ilike(pattern), 1),
             else_=2),
        Summary.id.desc()).limit(30)
    results = []
    for s in q.all():
        r = _row(s)
        r["snippet"] = (s.summary or "")[:300]
        results.append(r)
    return results
