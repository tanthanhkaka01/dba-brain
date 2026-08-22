import pyodbc

conn_str = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=192.0.2.245,1433;"
    "DATABASE=master;"
    "UID=dba_user;"
    "PWD={T!*9}}f0l;]#?O}}ET};"
    "Encrypt=no;"
    "TrustServerCertificate=yes;"
    "Connection Timeout=10;"
)

try:
    print("Connecting...")

    conn = pyodbc.connect(conn_str)

    print("Connected OK")

    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            @@SERVERNAME,
            @@VERSION
    """)

    row = cursor.fetchone()

    print("\nSERVER:")
    print(row[0])

    print("\nVERSION:")
    print(row[1])

    conn.close()

except Exception as e:
    print("\nERROR:")
    print(e)
