from server.app import app

paths = []
for r in app.routes:
    methods = getattr(r, 'methods', None) or set()
    path = getattr(r, 'path', '')
    paths.append((sorted(methods), path))

for m, p in sorted(paths, key=lambda x: x[1]):
    print(m, p)
