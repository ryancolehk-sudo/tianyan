"""Serve a prebuilt SPA without Node, a dev server, or an external proxy."""
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse


def mount_static(app, directory):
    root = Path(directory).resolve(strict=True)
    if not (root / 'index.html').is_file():
        raise RuntimeError('Built web application is missing')

    @app.get('/{path:path}', include_in_schema=False)
    def static_file(path: str):
        if path == 'api' or path.startswith(('api/', 'health/', 'native/')):
            raise HTTPException(404)
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            raise HTTPException(404)
        if candidate.is_file():
            response = FileResponse(candidate)
            if path.startswith('assets/'):
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        elif Path(path).suffix:
            raise HTTPException(404)
        else:
            response = FileResponse(root / 'index.html')
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response
