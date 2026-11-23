"""Cloud display permission for shared, externally sourced market records."""
from sqlalchemy import or_

from app.config import settings


def cloud_share_allowed(provider):
    if settings().deployment_mode != 'cloud' or provider.kind == 'manual':
        return True
    rights = provider.rights
    return (rights.get('redistribute') if isinstance(rights, dict) else rights.redistribute) == 'allowed'


def cloud_share_sql():
    from app.models import Provider
    return True if settings().deployment_mode != 'cloud' else or_(
        Provider.kind == 'manual', Provider.rights['redistribute'].astext == 'allowed')
