from alembic import context

from app.db import Base, engine
from app import models  # noqa: F401
from app.evidence import models as evidence_models  # noqa: F401
from app.research import models as research_models  # noqa: F401
from app.observations import models as observation_models  # noqa: F401
from app.validations import models as validation_models  # noqa: F401
from app.review_archives import models as review_archive_models  # noqa: F401
from app.news_center import models as news_center_models  # noqa: F401
from app.chatgpt import models as chatgpt_models  # noqa: F401
from app import read_cache_models  # noqa: F401
from app.market import resilience_models  # noqa: F401
from app.market import read_facets  # noqa: F401
from app.system_settings import models as system_settings_models  # noqa: F401
from app.tomorrow import models as tomorrow_models  # noqa: F401

with engine().connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
