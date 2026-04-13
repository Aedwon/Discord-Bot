"""
Vercel Serverless Function: /api/rankings
Returns all-time top raffles and events for the analytics dashboard.
Queries the database directly (no bot connection needed).
"""

from http.server import BaseHTTPRequestHandler
import json
import pymysql
import pymysql.cursors
import os


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        try:
            db_port = os.environ.get('DB_PORT', '3306')
            try:
                db_port_int = int(db_port)
            except ValueError:
                db_port_int = 3306

            connection = pymysql.connect(
                host=os.environ.get('DB_HOST', ''),
                port=db_port_int,
                user=os.environ.get('DB_USER', ''),
                password=os.environ.get('DB_PASSWORD', ''),
                database=os.environ.get('DB_NAME', ''),
                cursorclass=pymysql.cursors.DictCursor
            )

            with connection.cursor() as cursor:
                # Top 5 raffles by total entries (all time)
                cursor.execute('''
                    SELECT er.raffle_id, r.title, COUNT(*) as total_entries
                    FROM event_raffle_entries er
                    JOIN event_raffles r ON r.id = er.raffle_id
                    GROUP BY er.raffle_id, r.title
                    ORDER BY total_entries DESC LIMIT 5
                ''')
                top_raffles = cursor.fetchall()

                # Top 5 events by total registrations (all time)
                cursor.execute('''
                    SELECT ere.event_id, COALESCE(er.title, CONCAT('Event #', ere.event_id)) as title, COUNT(*) as total_participants
                    FROM event_registration_entries ere
                    LEFT JOIN event_registrations er ON er.event_id = ere.event_id
                    GROUP BY ere.event_id, er.title
                    ORDER BY total_participants DESC LIMIT 5
                ''')
                top_events = cursor.fetchall()

                # Top 5 Speedsters (Avg Time, min 5 answers)
                cursor.execute('''
                    SELECT qal.user_id, COALESCE(mn.display_name, CONCAT('User ', qal.user_id)) as name, 
                           AVG(qal.time_taken) as avg_time, COUNT(*) as correct_answers
                    FROM quiz_answer_logs qal
                    LEFT JOIN member_names mn ON mn.user_id = qal.user_id
                    GROUP BY qal.user_id, mn.display_name
                    HAVING correct_answers >= 5
                    ORDER BY avg_time ASC LIMIT 5
                ''')
                top_speedsters = cursor.fetchall()

                # Top 5 Streaks
                cursor.execute('''
                    SELECT qus.user_id, COALESCE(mn.display_name, CONCAT('User ', qus.user_id)) as name, qus.max_streak
                    FROM quiz_user_streaks qus
                    LEFT JOIN member_names mn ON mn.user_id = qus.user_id
                    ORDER BY qus.max_streak DESC LIMIT 5
                ''')
                top_streaks = cursor.fetchall()

                # Top 5 Hardest Questions
                cursor.execute('''
                    SELECT question_id, question_text, 
                           (1 - (times_correct / times_asked)) * 100 as failure_rate
                    FROM quiz_question_stats
                    WHERE times_asked >= 3
                    ORDER BY failure_rate DESC LIMIT 5
                ''')
                hardest_qs = cursor.fetchall()

                # Global Quiz Stats
                cursor.execute('SELECT AVG(time_taken) as avg_time, COUNT(*) as total_correct FROM quiz_answer_logs')
                quiz_global = cursor.fetchone()

            connection.close()

            # Safely cast BigInts → strings and counts → ints for JSON serialization
            def safe_row(row):
                if not row: return row
                result = {}
                for k, v in row.items():
                    if k in ('raffle_id', 'event_id', 'user_id', 'max_streak', 'total_entries', 'total_participants', 'correct_answers', 'total_correct'):
                        result[k] = int(v) if v is not None else 0
                    elif k in ('avg_time', 'failure_rate'):
                        result[k] = float(v) if v is not None else 0.0
                    else:
                        result[k] = v
                return result

            response = {
                "success": True,
                "top_raffles": [safe_row(r) for r in top_raffles],
                "top_events": [safe_row(r) for r in top_events],
                "top_speedsters": [safe_row(r) for r in top_speedsters],
                "top_streaks": [safe_row(r) for r in top_streaks],
                "hardest_questions": [safe_row(r) for r in hardest_qs],
                "quiz_global": safe_row(quiz_global)
            }
            self.wfile.write(json.dumps(response, default=str).encode('utf-8'))

        except Exception as e:
            error_response = {"success": False, "error": str(e)}
            self.wfile.write(json.dumps(error_response).encode('utf-8'))
