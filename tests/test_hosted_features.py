import os
import sqlite3
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from unittest.mock import patch
import unittest
import test_public_beta as helpers
from hosted_dashboard import week_view
from free_time_ideas import recommend
from assignments_service import parse_rows, preparation_tasks

class HostedFeaturesTests(unittest.TestCase):
    setUp = helpers.PublicBetaTests.setUp
    tearDown = helpers.PublicBetaTests.tearDown
    post = helpers.PublicBetaTests.post
    register = helpers.PublicBetaTests.register

    def query(self, sql, args=()):
        with sqlite3.connect(os.path.join(self.temp.name, 'public_beta.sqlite3')) as db:
            result = db.execute(sql,args).fetchall()
        return result

    def new_task(self, name='Task', **extra):
        self.post('/tasks', {'name':name,'hours':'2','importance':'4','difficulty':'5',
                            'deadline':(datetime.now()+timedelta(days=3)).strftime('%Y-%m-%dT%H:%M'),**extra})
        return self.query('SELECT id FROM tasks WHERE name=?',(name,))[0][0]

    def test_batch_update_is_atomic_and_private(self):
        self.register('one@example.com')
        first=self.new_task('A');second=self.new_task('B')
        self.post('/tasks/update',{'task_id':[first,second],'completed':[first,second]})
        self.assertEqual(self.query('SELECT SUM(completed) FROM tasks')[0][0],2)
        other=self.app.test_client();self.register('two@example.com',other)
        self.new_task('C')
        self.assertEqual(self.post('/tasks/update',{'task_id':[first,second],'completed':[]},other).status_code,404)
        self.assertEqual(self.query('SELECT SUM(completed) FROM tasks')[0][0],2)
        self.post('/tasks/update',{'task_id':[first,second],'completed':[second]})
        self.assertEqual(self.query('SELECT completed FROM tasks WHERE id=?',(first,))[0][0],0)

    def test_weekly_rollover_and_edit(self):
        self.register('one@example.com')
        self.post('/settings',{'timezone':'America/New_York','work_start':'09:00','work_end':'21:00'})
        task_id=self.new_task('Weekly',repeat='weekly',weekday='Sunday',due_time='20:00',deadline='')
        self.assertEqual(self.query('SELECT repeat,difficulty FROM tasks')[0],('weekly',5))
        self.query("UPDATE tasks SET due_utc='2020-01-05T20:00:00-05:00',completed=1 WHERE id=?",(task_id,))
        self.client.get('/dashboard')
        due, completed=self.query('SELECT due_utc,completed FROM tasks')[0]
        local=datetime.fromisoformat(due).astimezone(ZoneInfo('America/New_York'))
        self.assertEqual((local.weekday(),local.hour,completed),(6,20,0))
        self.post('/tasks/'+task_id+'/edit',{'name':'Weekly','hours':'1','importance':'3','difficulty':'2','repeat':'weekly','weekday':'Tuesday','due_time':'19:00'})
        self.assertEqual(self.query('SELECT weekday,due_time,difficulty FROM tasks')[0],('Tuesday','19:00',2))

    def test_completed_tasks_clear_from_display_after_week(self):
        self.register('one@example.com');task_id=self.new_task('Old complete')
        self.query('UPDATE tasks SET completed=1,completed_at=? WHERE id=?',((datetime.now(ZoneInfo('UTC'))-timedelta(days=8)).isoformat(),task_id))
        self.assertNotIn(b'Old complete',self.client.get('/dashboard').data)
        self.assertEqual(len(self.query('SELECT id FROM tasks')),1)

    def test_break_ideas_are_private_and_validated(self):
        self.register('one@example.com')
        self.post('/break-ideas',{'name':'Private idea','minutes':'10','category':'Music','note':'Practice'})
        self.assertEqual(len(self.query('SELECT id FROM break_ideas')),1)
        self.assertIn(b'Private idea',self.client.get('/dashboard').data)
        self.post('/break-ideas',{'name':'Invalid','minutes':'0'})
        self.assertEqual(len(self.query('SELECT id FROM break_ideas')),1)
        other=self.app.test_client();self.register('two@example.com',other)
        self.assertNotIn(b'Private idea',other.get('/dashboard').data)

    def test_schedule_clips_busy_events_and_transparent_events(self):
        zone=ZoneInfo('America/New_York');now=datetime(2026,9,29,10,tzinfo=zone)
        def event(title,start,end,free=False):
            return dict(title=title,start=now.replace(hour=start),end=now.replace(hour=end),free=free,all_day=False)
        events=[event('Busy',9,11),event('Overlap',10,12),event('Free lunch',13,14,True),event('Later',14,15)]
        hours={'Tuesday':['09:00','17:00']}
        result=week_view(events,hours,now)
        self.assertEqual([(a.hour,b.hour) for a,b in result['days'][0]['blocks']],[(12,14),(15,17)])
        self.assertEqual(result['today_free_hours'],4)
        ideas=recommend([(now,now+timedelta(minutes=15)),(now+timedelta(hours=1),now+timedelta(hours=3))],
                        [{'name':'fits','minutes':15},{'name':'too long','minutes':20}])
        self.assertEqual([idea['name'] for idea in ideas],['fits'])

    def test_busy_all_day_event_reserves_entire_day(self):
        now=datetime(2026,9,29,10,tzinfo=ZoneInfo('America/New_York'))
        events=[dict(title='Out of office',start=now.replace(hour=0),end=now.replace(hour=0)+timedelta(days=1),all_day=True,free=False)]
        self.assertEqual(week_view(events,{'Tuesday':['09:00','17:00']},now)['today_free_hours'],0)

    def test_assignment_classification_release_and_timezone(self):
        zone=ZoneInfo('America/Los_Angeles');now=datetime(2026,9,29,10,tzinfo=zone)
        rows=[['Assignment','Due Date','Course','Type'],['Finance exam','10/10/2026','FNCE 4305','Exam'],['Econ midterm','10/10/2026','ECON 2411','Exam'],['Report','10/15/2026','MKTG','Project']]
        items=parse_rows(rows,'test',now=now,zone=zone)
        self.assertEqual(str(items[0]['due'].tzinfo),'America/Los_Angeles')
        tasks=preparation_tasks(items,now)
        self.assertEqual([t['course'] for t in tasks],['FNCE 4305'])
        self.assertEqual(items[2]['prep_start'].date().isoformat(),'2026-10-08')

    def test_existing_database_migrates_without_data_loss(self):
        self.register('one@example.com');self.new_task('Preserved')
        from public_beta import create_app
        second=create_app({'TESTING':True,'SECRET_KEY':'test-only-key','DATA_DIR':self.temp.name,'SIGNUP_CODE':'invite'})
        self.assertEqual(len(self.query('SELECT id FROM users')),1)
        self.assertEqual(self.query('SELECT name FROM tasks')[0][0],'Preserved')
        self.assertEqual(second.test_client().get('/healthz').status_code,200)

    def test_forms_have_csrf_and_all_sections_render(self):
        self.register('one@example.com')
        text=self.client.get('/dashboard').data.decode()
        for heading in ('Suggested task times','For your next break','AI recommendation','Assignments from Google Sheets','Free time today','Free time tomorrow','schedule-next','difficulty','Weekly'):
            self.assertIn(heading,text)
        from html.parser import HTMLParser
        class Forms(HTMLParser):
            def __init__(self):super().__init__();self.forms=[];self.current=None
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=='form':self.current=[attrs.get('method'),False]
                if tag=='input' and attrs.get('name')=='csrf_token' and self.current:self.current[1]=True
            def handle_endtag(self,tag):
                if tag=='form':self.forms.append(self.current);self.current=None
        parser=Forms();parser.feed(text)
        self.assertTrue(all(csrf for method,csrf in parser.forms if method=='post'))

    def test_sheets_oauth_import_is_private_and_deduplicated(self):
        from public_beta import create_app
        from cryptography.fernet import Fernet
        self.app=create_app({'TESTING':True,'SECRET_KEY':'test-only-key','DATA_DIR':self.temp.name,'SIGNUP_CODE':'invite',
                             'GOOGLE_CLIENT_ID':'client','GOOGLE_CLIENT_SECRET':'secret','PUBLIC_BASE_URL':'https://example.com',
                             'TOKEN_ENCRYPTION_KEY':Fernet.generate_key().decode()})
        self.client=self.app.test_client();self.register('one@example.com')
        self.post('/sheets/configure',{'url':'https://docs.google.com/spreadsheets/d/testsheet/edit','tab':'Assignments'})
        self.post('/sheets/connect',{})
        with self.client.session_transaction() as state:oauth=state['sheets_oauth']['state']
        self.assertEqual(self.client.get('/sheets/callback?state=bad&code=secret').status_code,400)
        self.post('/sheets/connect',{})
        with self.client.session_transaction() as state:oauth=state['sheets_oauth']['state']
        with patch('public_beta.exchange_code',return_value='private-sheets-token'):
            self.assertEqual(self.client.get('/sheets/callback?state='+oauth+'&code=secret').status_code,302)
        self.assertNotIn('private-sheets-token',self.query('SELECT encrypted_refresh_token FROM sheets_connections')[0][0])
        due=(datetime.now()+timedelta(days=5)).strftime('%Y-%m-%d')
        data=[['Assignment','Due Date','Course','Type'],['Finance test',due,'FNCE','Exam'],['Econ test',due,'ECON 2411','Exam']]
        with patch('public_beta.refresh_access_token',return_value='access'),patch('public_beta.fetch_rows',return_value=data):
            response=self.client.get('/dashboard')
            self.assertIn(b'Study for Finance test',response.data)
            self.assertNotIn(b'Study for Econ test',response.data)
            self.client.get('/dashboard')
            self.assertEqual(len(self.query("SELECT id FROM tasks WHERE source='sheet'")),1)
            task_id=self.query("SELECT id FROM tasks WHERE source='sheet'")[0][0]
            self.post('/tasks/'+task_id+'/delete',{})
            self.client.get('/dashboard')
            self.assertEqual(len(self.query("SELECT id FROM tasks WHERE source='sheet'")),0)
            other=self.app.test_client();self.register('two@example.com',other)
            self.assertNotIn(b'Finance test',other.get('/dashboard').data)
        self.post('/sheets/disconnect',{})
        self.assertEqual(self.query('SELECT user_id FROM sheets_connections'),[])

    def test_calendar_color_and_history_are_retained(self):
        from hosted_calendar import upcoming_events
        from unittest.mock import Mock
        zone=ZoneInfo('America/New_York');now=datetime(2026,9,29,16,tzinfo=zone)
        palette=Mock(ok=True);palette.json.return_value={'event':{'11':{'background':'#dc2127'}}}
        response=Mock();response.json.return_value={'items':[{'summary':'Morning class','colorId':'11',
            'start':{'dateTime':'2026-09-29T09:00:00-04:00'},'end':{'dateTime':'2026-09-29T10:00:00-04:00'}}]}
        with patch('hosted_calendar.requests.get',side_effect=[palette,response]):
            events=upcoming_events('token',zone,now)
        self.assertEqual(events[0]['color'],'#dc2127')
        self.assertEqual(events[0]['start'].hour,9)
