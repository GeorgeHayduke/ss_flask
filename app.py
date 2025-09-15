# app.py (very top, before any matplotlib.pyplot import)
import matplotlib
matplotlib.use("Agg")   # << headless, no macOS GUI
import matplotlib.pyplot as plt


from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta, date
import io, base64


app = Flask(__name__)
app.config["SECRET_KEY"] = "dev"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///progress.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

LIFTS = ["Squat", "Bench Press", "Overhead Press", "Deadlift"]
ACCESSORIES = {
    "A": [("Barbell Curl", "3x10"), ("Lat Pulldown", "3x10")],
    "B": [("Triceps Pushdown", "3x12"), ("Barbell Curl", "3x10")],
}

# ---------------- Models ----------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Program(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer)
    weeks = db.Column(db.Integer, default=6)
    start_date = db.Column(db.Date)
    workout_days_per_week = db.Column(db.Integer)
    increment_lbs = db.Column(db.Integer, default=5)

class Lift(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer)
    name = db.Column(db.String(64))
    start_weight = db.Column(db.Integer)
    current_weight = db.Column(db.Integer)
    increment_lbs = db.Column(db.Integer, default=5)

class Session(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer)
    date = db.Column(db.Date)
    workout_type = db.Column(db.String(1))  # 'A' or 'B'
    completed = db.Column(db.Boolean, default=False)

class Set(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer)
    lift_name = db.Column(db.String(64))
    set_index = db.Column(db.Integer)
    weight = db.Column(db.Integer)
    reps = db.Column(db.Integer)
    is_warmup = db.Column(db.Boolean, default=False)

with app.app_context():
    db.create_all()
    if not User.query.first():
        db.session.add(User())
        db.session.commit()

# ---------------- Helpers ----------------
def round_to_5(x): return int(round(x/5.0)*5)

def default_warmups(work_weight, lift_name):
    """~50% x5, ~70% x3, ~90% x(1 DL, 2 others). No bar sets for DL; optional bar sets if <=135 for others."""
    ww = max(work_weight, 45)
    is_dl = (lift_name == "Deadlift")
    warmups = []
    if not is_dl and ww <= 135:
        warmups += [(45, 5), (45, 5)]
    scheme = [(0.50, 5), (0.70, 3), (0.90, 1 if is_dl else 2)]
    for p, reps in scheme:
        w = max(45, round_to_5(ww * p))
        if w < ww and (not warmups or warmups[-1][0] != w):
            warmups.append((w, reps))
    return warmups

def ensure_program_and_lifts():
    user = User.query.first()
    prog = Program.query.filter_by(user_id=user.id).first()
    lifts = {l.name: l for l in Lift.query.filter_by(user_id=user.id).all()}
    return user, prog, lifts

def schedule_plan(user_id, weeks, start_date, wkpw):
    # clear old
    old_sessions = Session.query.filter_by(user_id=user_id).all()
    for s in old_sessions:
        Set.query.filter_by(session_id=s.id).delete()
    Session.query.filter_by(user_id=user_id).delete()
    db.session.commit()

    total_sessions = weeks * wkpw
    types = ["A" if i % 2 == 0 else "B" for i in range(total_sessions)]
    d = start_date
    for _type in types:
        db.session.add(Session(user_id=user_id, date=d, workout_type=_type, completed=False))
        d += timedelta(days=2 if wkpw == 3 else 1)
    db.session.commit()

def simulate_expected_progress(start_weights, inc, weeks, wkpw):
    curr = start_weights.copy()
    sessions = weeks * wkpw
    schedule = ["A" if i % 2 == 0 else "B" for i in range(sessions)]
    rows = []
    for s_idx, t in enumerate(schedule, start=1):
        if t == "A":
            for lift in ["Squat", "Bench Press", "Deadlift"]:
                curr[lift] = round_to_5(curr[lift] + inc)
        else:
            for lift in ["Squat", "Overhead Press", "Deadlift"]:
                curr[lift] = round_to_5(curr[lift] + inc)
        if s_idx % wkpw == 0:
            rows.append({
                "Week": s_idx // wkpw,
                "Squat": curr["Squat"],
                "Bench": curr["Bench Press"],
                "Press": curr["Overhead Press"],
                "Deadlift": curr["Deadlift"],
            })
    return rows  # list of dicts for Jinja

def generate_sets_for_session(session_obj, lifts):
    Set.query.filter_by(session_id=session_obj.id).delete()
    plan = []
    def add_ex(name, ww, sets, reps):
        for w,r in default_warmups(ww, name):
            plan.append((name, len(plan), int(w), int(r), True))
        for _ in range(sets):
            plan.append((name, len(plan), int(ww), int(reps), False))
    if session_obj.workout_type == "A":
        add_ex("Squat", lifts["Squat"].current_weight, 3, 5)
        add_ex("Bench Press", lifts["Bench Press"].current_weight, 3, 5)
        add_ex("Deadlift", lifts["Deadlift"].current_weight, 1, 5)
    else:
        add_ex("Squat", lifts["Squat"].current_weight, 3, 5)
        add_ex("Overhead Press", lifts["Overhead Press"].current_weight, 3, 5)
        add_ex("Deadlift", lifts["Deadlift"].current_weight, 1, 5)
    for (name, idx, w, r, wu) in plan:
        db.session.add(Set(session_id=session_obj.id, lift_name=name, set_index=idx, weight=w, reps=r, is_warmup=wu))
    db.session.commit()

def mark_complete_and_progress(session_obj, prog, lifts):
    session_obj.completed = True
    trained = []
    if session_obj.workout_type == "A":
        trained = ["Squat", "Bench Press", "Deadlift"]
    else:
        trained = ["Squat", "Overhead Press", "Deadlift"]
    for name in trained:
        l = lifts[name]
        l.current_weight = round_to_5(l.current_weight + prog.increment_lbs)
    db.session.commit()

def lift_series_for_charts(user_id):
    # Build {lift: [(date, top_work_weight), ...]}
    out = {k: [] for k in LIFTS}
    sessions = Session.query.filter_by(user_id=user_id, completed=True).order_by(Session.date).all()
    for s in sessions:
        # top non-warmup set per lift in that session
        nonwu = db.session.query(Set.lift_name, db.func.max(Set.weight)).filter_by(session_id=s.id, is_warmup=False).group_by(Set.lift_name).all()
        for name, w in nonwu:
            out[name].append((s.date, int(w)))
    return out

def fig_to_base64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("ascii")
    plt.close(fig)
    return "data:image/png;base64," + b64

# ---------------- Routes ----------------
@app.route("/")
def dashboard():
    user, prog, lifts = ensure_program_and_lifts()
    return render_template("dashboard.html", prog=prog, lifts=lifts, accessories=ACCESSORIES)

@app.route("/setup", methods=["GET", "POST"])
def setup():
    user, prog, lifts = ensure_program_and_lifts()

    if request.method == "POST":
        start_date = request.form.get("start_date", date.today().isoformat())
        wkpw = int(request.form.get("wkpw", 3))
        inc = int(request.form.get("inc", 5))
        sq = int(request.form.get("squat", 135))
        bp = int(request.form.get("bench", 95))
        ohp = int(request.form.get("press", 65))
        dl = int(request.form.get("deadlift", 155))

        if not prog:
            prog = Program(user_id=user.id)
        prog.weeks = 6
        prog.start_date = datetime.fromisoformat(start_date).date()
        prog.workout_days_per_week = wkpw
        prog.increment_lbs = inc
        db.session.add(prog)

        # upsert lifts
        name_map = {"Squat": sq, "Bench Press": bp, "Overhead Press": ohp, "Deadlift": dl}
        for name, start_w in name_map.items():
            lift = Lift.query.filter_by(user_id=user.id, name=name).first()
            if not lift:
                lift = Lift(user_id=user.id, name=name, start_weight=start_w, current_weight=start_w, increment_lbs=inc)
            else:
                lift.start_weight = start_w
                lift.current_weight = start_w
                lift.increment_lbs = inc
            db.session.add(lift)
        db.session.commit()

        schedule_plan(user.id, prog.weeks, prog.start_date, prog.workout_days_per_week)
        return redirect(url_for("setup"))

    # GET: show form + projection
    draft_start = (prog.start_date if prog else date.today())
    draft_wkpw = (prog.workout_days_per_week if prog else 3)
    draft_inc  = (prog.increment_lbs if prog else 5)
    start_weights = {
        "Squat":          lifts.get("Squat").start_weight if lifts.get("Squat") else 135,
        "Bench Press":    lifts.get("Bench Press").start_weight if lifts.get("Bench Press") else 95,
        "Overhead Press": lifts.get("Overhead Press").start_weight if lifts.get("Overhead Press") else 65,
        "Deadlift":       lifts.get("Deadlift").start_weight if lifts.get("Deadlift") else 155,
    }
    projection_rows = simulate_expected_progress(start_weights, draft_inc, 6, draft_wkpw)
    return render_template("setup.html",
                           prog=prog, lifts=lifts,
                           draft_start=draft_start, draft_wkpw=draft_wkpw, draft_inc=draft_inc,
                           start_weights=start_weights, projection_rows=projection_rows)

@app.route("/session", methods=["GET", "POST"])
def session_view():
    user, prog, lifts = ensure_program_and_lifts()
    if not prog:
        return redirect(url_for("setup"))

    upcoming = Session.query.filter_by(user_id=user.id, completed=False).order_by(Session.date).first()
    if not upcoming:
        return render_template("session.html", prog=prog, lifts=lifts, session=None, sets=[], accessories=[])

    if request.method == "POST":
        action = request.form.get("action")
        if action == "generate":
            generate_sets_for_session(upcoming, lifts)
        elif action == "complete":
            mark_complete_and_progress(upcoming, prog, lifts)
        return redirect(url_for("session_view"))

    sets = Set.query.filter_by(session_id=upcoming.id).order_by(Set.set_index).all()
    acc = ACCESSORIES[upcoming.workout_type]
    # Group sets by lift
    by_lift = {}
    for s in sets:
        by_lift.setdefault(s.lift_name, []).append(s)
    return render_template("session.html", prog=prog, lifts=lifts, session=upcoming, sets_by_lift=by_lift, accessories=acc)

@app.route("/progress")
def progress():
    user, prog, lifts = ensure_program_and_lifts()
    series = lift_series_for_charts(user.id)
    charts = {}
    for lift, pts in series.items():
        if not pts: continue
        fig, ax = plt.subplots()
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax.plot(xs, ys, marker="o")  # no explicit colors/style
        ax.set_xlabel("Date"); ax.set_ylabel("Top Set (lbs)"); ax.set_title(lift)
        charts[lift] = fig_to_base64(fig)
    return render_template("progress.html", charts=charts)

if __name__ == "__main__":
    app.run(debug=True)


