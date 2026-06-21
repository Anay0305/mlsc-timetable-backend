# mlsc-timetable-backend

Backend for the MLSC timetable website and mobile app.

## Overview

`mlsc-timetable-backend` is the backend layer for parsing, storing, and serving Thapar timetable data.

The current codebase focuses on the timetable parser. It reads spreadsheet-based timetable layouts and extracts structured class data grouped by batch and day. The parser captures slot timings, subject codes and names, class type, elective options, confidence metadata, raw source values, and spreadsheet cell bounds for validation.

Planned backend work includes API endpoints, PostgreSQL database integration, and a clean ingestion flow from parsed spreadsheet data to production-ready timetable records.

## Current Scope

- Thapar timetable spreadsheet parsing
- Subject catalog lookup from `assets/subjects.json`
- Structured output models for batches, days, classes, and elective groups
- JSON serialization helpers for parsed timetable data

## Planned Work

- REST API endpoints for timetable access
- PostgreSQL-backed persistence
- Parser-to-database ingestion pipeline

## Setup

Requires Python 3.11+.

```bash
python -m pip install -e .
```
