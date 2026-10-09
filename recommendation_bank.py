"""Account-owned saved break activities and optional starter suggestions."""
from uuid import uuid4
from flask import abort, flash, g, redirect, render_template, request, url_for

CATEGORIES = ('Wellness', 'Study', 'Career', 'Music', 'Social', 'Other')

def register_recommendation_bank(app, db, login_required):
    def owned(idea_id):
        row = db().execute('SELECT * FROM break_ideas WHERE id=? AND user_id=?', (idea_id, g.user['id'])).fetchone()
        if row is None:
            abort(404)
        return row

    @app.get('/recommendation-bank')
    @login_required
    def recommendation_bank():
        ideas = [dict(row) for row in db().execute('SELECT * FROM break_ideas WHERE user_id=? ORDER BY name COLLATE NOCASE', (g.user['id'],))]
        return render_template('public/recommendation_bank.html', ideas=ideas, categories=CATEGORIES)

    @app.post('/recommendation-bank/save')
    @login_required
    def save_bank_idea():
        idea_id = request.form.get('id', '')
        if idea_id:
            owned(idea_id)
        name = request.form.get('name', '').strip()
        note = request.form.get('note', '').strip()
        category = request.form.get('category', 'Other')
        try:
            minutes = int(request.form.get('minutes', ''))
            if not 1 <= minutes <= 240 or not 0 < len(name) <= 100 or len(note) > 200 or category not in CATEGORIES:
                raise ValueError()
        except ValueError:
            flash('Enter an activity, 1–240 minutes, and a valid category. Notes may be up to 200 characters.', 'error')
        else:
            with db():
                if idea_id:
                    db().execute('UPDATE break_ideas SET name=?,minutes=?,category=?,note=? WHERE id=? AND user_id=?', (name, minutes, category, note, idea_id, g.user['id']))
                else:
                    db().execute('INSERT INTO break_ideas VALUES (?,?,?,?,?,?)', (str(uuid4()), g.user['id'], name, minutes, category, note))
            flash('Activity saved.', 'success')
        return redirect(url_for('recommendation_bank'))

    @app.post('/recommendation-bank/<idea_id>/delete')
    @login_required
    def delete_bank_idea(idea_id):
        owned(idea_id)
        with db():
            db().execute('DELETE FROM break_ideas WHERE id=? AND user_id=?', (idea_id, g.user['id']))
        flash('Activity removed.', 'success')
        return redirect(url_for('recommendation_bank'))

    @app.post('/recommendation-bank/import')
    @login_required
    def import_bank_ideas():
        activities = []
        try:
            for line in request.form.get('activities', '').splitlines():
                if not line.strip():
                    continue
                name, raw_minutes = line.rsplit('|', 1)
                name, minutes = name.strip(), int(raw_minutes.strip())
                if not 0 < len(name) <= 100 or not 1 <= minutes <= 240:
                    raise ValueError()
                activities.append((name, minutes))
            if not activities or len(activities) > 100:
                raise ValueError()
        except ValueError:
            flash('Use one activity per line: Activity name | minutes. Import 1–100 activities, each lasting 1–240 minutes.', 'error')
            return redirect(url_for('recommendation_bank'))
        saved = {row['name'].casefold() for row in db().execute('SELECT name FROM break_ideas WHERE user_id=?', (g.user['id'],))}
        count = 0
        with db():
            for name, minutes in activities:
                if name.casefold() not in saved:
                    db().execute('INSERT INTO break_ideas VALUES (?,?,?,?,?,?)', (str(uuid4()), g.user['id'], name, minutes, 'Other', ''))
                    saved.add(name.casefold())
                    count += 1
        flash(f'{count} activities imported to your private bank.', 'success')
        return redirect(url_for('recommendation_bank'))
