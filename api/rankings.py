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
                    SELECT er.raffle_id,
                           r.title,
                           COUNT(*) as total_entries
                    FROM event_raffle_entries er
                    JOIN event_raffles r ON r.id = er.raffle_id
                    GROUP BY er.raffle_id, r.title
                    ORDER BY total_entries DESC
                    LIMIT 5
                ''')
                top_raffles = cursor.fetchall()

                # Top 5 events by total registrations (all time)
                cursor.execute('''
                    SELECT ere.event_id,
                           COALESCE(er.title, CONCAT('Event #', ere.event_id)) as title,
                           COUNT(*) as total_participants
                    FROM event_registration_entries ere
                    LEFT JOIN event_registrations er ON er.event_id = ere.event_id
                    GROUP BY ere.event_id, er.title
                    ORDER BY total_participants DESC
                    LIMIT 5
                ''')
                top_events = cursor.fetchall()

            connection.close()

            response = {
                "success": True,
                "top_raffles": top_raffles,
                "top_events": top_events,
            }
            self.wfile.write(json.dumps(response).encode('utf-8'))

        except Exception as e:
            error_response = {"success": False, "error": str(e)}
            self.wfile.write(json.dumps(error_response).encode('utf-8'))
