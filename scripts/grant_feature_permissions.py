#!/usr/bin/env python3
"""Grant permissions to PostgREST roles (anon, service_role) in openbrain-db."""
import subprocess
import sys

SQL = """
GRANT ALL ON TABLE public.pca_features TO anon, service_role;
GRANT ALL ON TABLE public.pca_feature_sets TO anon, service_role;
GRANT ALL ON TABLE public.pca_feature_set_members TO anon, service_role;
NOTIFY pgrst, 'reload schema';
"""

cmd = ["docker", "exec", "openbrain-db", "psql", "-U", "postgres", "-c", SQL]
r = subprocess.run(cmd, capture_output=True, text=True)
print(r.stdout)
if r.stderr:
    print(r.stderr, file=sys.stderr)

if r.returncode == 0:
    print("✅ Permissions granted to anon & service_role, PostgREST schema reloaded.")
sys.exit(r.returncode)
