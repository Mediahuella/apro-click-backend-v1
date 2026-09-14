"""Búsqueda difusa de empresas: extensiones pg_trgm y unaccent + índice GIN.

El buscador del panel hacía solo `LIKE`: un typo (``comerzializadora``) o un
acento (``ESTACION`` vs ``ESTACIÓN``) devolvía cero resultados aunque la
empresa existiera. Con ``pg_trgm`` la búsqueda tolera errores de tipeo vía
``word_similarity`` y con ``unaccent`` deja de importar la tilde.

El índice GIN es sobre ``lower(name)``, que sí es inmutable; ``unaccent()`` no
lo es y por eso no se indexa (con el volumen actual el filtro por acentos es
un seq scan barato).

Revision ID: 028
Revises: 027
Create Date: 2026-08-31
"""
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_companies_name_trgm "
        "ON companies USING gin (lower(name) gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_companies_name_trgm")
    # Las extensiones no se eliminan: pueden estar en uso por otros objetos.
