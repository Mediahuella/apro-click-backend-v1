"""Integraciones con los sistemas de APRO (DMS externo).

Compartido entre servicios porque el mismo dato se consulta desde más de un
stack: ``companies`` (sincronización diaria + checkout) y
``company-registration`` (alta de cliente nuevo).
"""
