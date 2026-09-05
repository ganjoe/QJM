#!/usr/bin/env python3
import psycopg2

conn = psycopg2.connect(host="localhost", port=5433, user="postgres", password="postgres", dbname="postgres")
cur = conn.cursor()

cur.execute("""
    SELECT grantee, table_name, privilege_type
    FROM information_schema.role_table_grants
    WHERE table_name IN ('pca_watchlists', 'pca_features', 'pca_feature_sets', 'pca_feature_set_members')
    ORDER BY table_name, grantee, privilege_type;
""")
for row in cur.fetchall():
    print(row)

# Also check RLS
cur.execute("""
    SELECT relname, relrowsecurity, relforcerowsecurity
    FROM pg_class
    WHERE relname IN ('pca_watchlists', 'pca_features', 'pca_feature_sets', 'pca_feature_set_members');
""")
print("\nRLS status:")
for row in cur.fetchall():
    print(row)

cur.close()
conn.close()
