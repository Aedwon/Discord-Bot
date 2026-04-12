from http.server import BaseHTTPRequestHandler
import json
import pymysql
import pymysql.cursors
import os

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Configure Headers & CORS
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        
        try:
            # Secure connection via Vercel Environment Variables
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
                # Fetch last 30 days of rollups securely (Read-Only)
                cursor.execute("SELECT * FROM analytics_daily_rollups ORDER BY date DESC LIMIT 30")
                rows = cursor.fetchall()
            
            connection.close()
            
            # Format types for JSON serialization
            for row in rows:
                if 'date' in row and row['date']:
                    row['date'] = str(row['date'])
                if 'granular_json' in row and row['granular_json']:
                    try:
                        row['granular_json'] = json.loads(row['granular_json'])
                    except Exception:
                        row['granular_json'] = {}
                        
            response = {"success": True, "data": rows}
            self.wfile.write(json.dumps(response).encode('utf-8'))
            
        except Exception as e:
            error_response = {"success": False, "error": str(e)}
            self.wfile.write(json.dumps(error_response).encode('utf-8'))
