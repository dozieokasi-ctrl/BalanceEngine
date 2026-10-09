import unittest
import test_public_beta as helpers

class RecommendationBankTests(unittest.TestCase):
    setUp = helpers.PublicBetaTests.setUp
    tearDown = helpers.PublicBetaTests.tearDown
    post = helpers.PublicBetaTests.post
    register = helpers.PublicBetaTests.register
    def test_bank_starters_edit_delete_and_isolation(self):
        self.register('bank@example.com')
        page = self.client.get('/recommendation-bank')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Your bank', page.data)
        self.post('/recommendation-bank/import', {'activities': 'Private activity A | 15\nPrivate activity B | 20'})
        self.post('/recommendation-bank/import', {'activities': 'Private activity A | 15\nPrivate activity B | 20'})
        import sqlite3
        from pathlib import Path
        with sqlite3.connect(Path(self.temp.name)/'public_beta.sqlite3') as db:
            ideas = db.execute('SELECT id,name FROM break_ideas').fetchall()
        self.assertEqual(len(ideas), 2)
        idea_id = ideas[0][0]
        other = self.app.test_client()
        self.register('other@example.com', other)
        other_page = other.get('/recommendation-bank').data
        self.assertIn(b'No saved activities yet', other_page)
        self.assertNotIn(b'Private activity A', other_page)
        self.assertNotIn(b'Earlier suggestions', other_page)
        self.assertEqual(self.post('/recommendation-bank/save', {'id':idea_id, 'name':'Stolen', 'minutes':'15', 'category':'Other'}, other).status_code, 404)
        self.assertEqual(self.post('/recommendation-bank/'+idea_id+'/delete', {}, other).status_code, 404)
        self.post('/recommendation-bank/save', {'id':idea_id, 'name':'My activity', 'minutes':'12', 'category':'Wellness', 'note':'Personal note'})
        self.assertIn(b'My activity', self.client.get('/recommendation-bank').data)
        self.post('/recommendation-bank/'+idea_id+'/delete', {})
        self.assertNotIn(b'My activity', self.client.get('/recommendation-bank').data)

    def test_bank_csrf_validation_and_login(self):
        self.assertEqual(self.client.get('/recommendation-bank').status_code, 302)
        self.register('valid@example.com')
        self.assertEqual(self.post('/recommendation-bank/save', {'name':'No csrf', 'minutes':'20'}, token=False).status_code, 400)
        self.post('/recommendation-bank/import', {'activities':'Valid | 20\ninvalid'})
        self.assertNotIn(b'<h3>Valid</h3>', self.client.get('/recommendation-bank').data)
        self.post('/recommendation-bank/save', {'name':'Invalid', 'minutes':'0', 'category':'Other'})
        self.assertNotIn(b'<h3>Invalid</h3>', self.client.get('/recommendation-bank').data)
