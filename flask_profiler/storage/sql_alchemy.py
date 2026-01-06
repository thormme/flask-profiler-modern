import json
from decimal import Decimal, ROUND_UP

import logging
import traceback
from typing import Union
from .base import BaseStorage
from datetime import datetime
import time
from sqlalchemy import Connection, create_engine, Text, Select, Update, Delete, event, text
from sqlalchemy import Column, Integer, Numeric, ScalarResult
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy import func
from sqlalchemy.pool import StaticPool
from sqlalchemy_utils import database_exists, create_database

logger = logging.getLogger("flask-profiler")
logger.setLevel(logging.INFO)

base = declarative_base()


def formatDate(timestamp, dateFormat):
    return datetime.fromtimestamp(timestamp).strftime(dateFormat)


class Measurements(base):
    __tablename__ = 'flask_profiler_measurements'

    id = Column(Integer, primary_key=True)
    startedAt = Column(Numeric)
    endedAt = Column(Numeric)
    elapsed = Column(Numeric(10, 4))
    method = Column(Text)
    args = Column(Text)
    kwargs = Column(Text)
    name = Column(Text)
    context = Column(Text)
    profileStats = Column(Text)

    def __repr__(self):
        return "<Measurements {}, {}, {}, {}, {}, {}, {}, {}, {}, {}>".format(
            self.id,
            self.startedAt,
            self.endedAt,
            self.elapsed,
            self.method,
            self.args,
            self.kwargs,
            self.name,
            self.context,
            self.profileStats
        )

class Metadata(base):
    __tablename__ = 'flask_profiler_metadata'

    id = Column(Integer, primary_key=True)
    last_retention_deletion_time = Column(Numeric)

    def __repr__(self):
        return "<Metadata {}, {}>".format(
            self.id,
            self.last_retention_deletion_date
        )

class LockableTransaction():
    def __init__(self, session: Session, is_sqlite: bool):
        self.is_sqlite = is_sqlite
        self.session = session
        self.in_transaction = False
        self.begin()
            
    def begin(self):
        if self.is_sqlite:
            self.session.execute(text("BEGIN EXCLUSIVE"))
        else:
            self.transaction = self.session.begin()
        self.in_transaction = True
            
    def commit(self) -> bool:
        if not self.in_transaction:
            return False
        
        if self.is_sqlite:
            self.session.execute(text("COMMIT"))
        else:
            self.transaction.commit()
            
        self.in_transaction = False
        return True
        
    def rollback(self):
        if not self.in_transaction:
            return False
        
        if self.is_sqlite:
            self.session.execute(text("ROLLBACK"))
        else:
            self.transaction.rollback()

        self.in_transaction = False
        return True
    
    def close(self, is_error: bool = False):
        if is_error:
            self.rollback()
        else:
            self.commit()
            
    def __enter__(self):
        return self
            
    def __exit__(self, exc_type, exc_value, traceback):
        is_error = not exc_type is None
        self.close(is_error)
        
    def __del__(self):
        self.close()

class Sqlalchemy(BaseStorage):

    def __init__(self, config=None):
        super(Sqlalchemy, self).__init__()
        self.config = config
        engine_kwargs = {}
        db_url = self.config.get("db_url", "sqlite:///flask_profiler.sql")
        DEFAULT_RETENTION_PERIOD_S = 2629743 # 1 month
        self.config['retention_period_s'] = float(self.config.get('retention_period_s', DEFAULT_RETENTION_PERIOD_S))
        self.config['retention_period_enabled'] = float(self.config.get('retention_period_enabled', False))

        self.is_sqlite = db_url.startswith("sqlite://")
        is_in_memory_sqlite: bool = db_url.startswith("sqlite:///:memory:") or db_url == "sqlite://"
        if is_in_memory_sqlite:
            engine_kwargs["poolclass"] = StaticPool
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        else:
            for k in ["pool_size", "max_overflow", "pool_recycle", "pool_timeout"]:
                v = self.config.get(k)
                if v is not None:
                    engine_kwargs[k] = v
            engine_kwargs["pool_pre_ping"] = self.config.get("pool_pre_ping", True)

        self.db = create_engine(db_url, **engine_kwargs)
        
        self.Session = sessionmaker(bind=self.db)
        self.create_database()

    def __enter__(self):
        return self

    def create_database(self):
        if not database_exists(self.db.url):
            create_database(self.db.url)
        
        base.metadata.create_all(self.db)
        
        # Add the metadata row if it does not exist
        with self.Session() as session:
            with self.begin_lockable_transaction(session) as locked_transaction:
                last_retention_deletion_time_sql: Select[float] = Select(Metadata.last_retention_deletion_time).with_for_update(nowait=True, of=Metadata.last_retention_deletion_time)
                last_retention_deletion_time: ScalarResult[float] = session.execute(last_retention_deletion_time_sql).scalar_one_or_none()
                if last_retention_deletion_time is None:
                    try:
                        session.add(Metadata(last_retention_deletion_time=0))
                        locked_transaction.commit()
                        session.commit()
                    except Exception as e:
                        print(e)
                        locked_transaction.rollback()
                        session.rollback()
                        return False

    def insert(self, kwds):
        endedAt = int(kwds.get('endedAt', None))
        startedAt = int(kwds.get('startedAt', None))
        elapsed = Decimal(kwds.get('elapsed', None))
        if elapsed:
            elapsed = elapsed.quantize(Decimal('.0001'), rounding=ROUND_UP)
        args = json.dumps(list(kwds.get('args', ())))  # tuple -> list -> json
        kwargs = json.dumps(kwds.get('kwargs', ()))
        context = json.dumps(kwds.get('context', {}))
        method = kwds.get('method', None)
        name = kwds.get('name', None)
        profileStats = kwds.get('profileStats', None)

        with self.Session() as session:
            try:
                session.add(Measurements(
                    endedAt=endedAt,
                    startedAt=startedAt,
                    elapsed=elapsed,
                    args=args,
                    kwargs=kwargs,
                    context=context,
                    method=method,
                    name=name,
                    profileStats=profileStats,
                ))
                session.commit()
            except Exception:
                session.rollback()
                raise

    @staticmethod
    def getFilters(kwargs):
        filters = {}
        filters["sort"] = kwargs.get('sort', "endedAt,desc").split(",")

        # because inserting and filtering may take place at the same moment,
        # a very little increment(0.5) is needed to find inserted
        # record by sql.
        filters["endedAt"] = float(
            kwargs.get('endedAt', time.time() + 0.5))
        filters["startedAt"] = float(
            kwargs.get('startedAt', time.time() - 3600 * 24 * 7))

        filters["elapsed"] = kwargs.get('elapsed', None)
        filters["method"] = kwargs.get('method', None)
        filters["name"] = kwargs.get('name', None)
        filters["args"] = json.dumps(
            list(kwargs.get('args', ())))  # tuple -> list -> json
        filters["kwargs"] = json.dumps(kwargs.get('kwargs', ()))
        filters["sort"] = kwargs.get('sort', "endedAt,desc").split(",")
        filters["skip"] = int(kwargs.get('skip', 0))
        filters["limit"] = int(kwargs.get('limit', 100))
        return filters

    def filter(self, kwds={}):
        # Find Operation
        f = Sqlalchemy.getFilters(kwds)
        with self.Session() as session:
            query = session.query(Measurements)

            if f["endedAt"]:
                query = query.filter(Measurements.endedAt <= f["endedAt"])
            if f["startedAt"]:
                query = query.filter(Measurements.startedAt >= f["startedAt"])
            if f["elapsed"]:
                query = query.filter(Measurements.elapsed >= f["elapsed"])
            if f["method"]:
                query = query.filter(Measurements.method == f["method"])
            if f["name"]:
                query = query.filter(Measurements.name == f["name"])

            if f["sort"][1] == 'desc':
                query = query.order_by(getattr(Measurements, f["sort"][0]).desc())
            else:
                query = query.order_by(getattr(Measurements, f["sort"][0]).asc())
            rows = query.limit(f['limit']).offset(f['skip']).all()
            result = [Sqlalchemy.jsonify_row(row) for row in rows]
        return (r for r in result)

    @staticmethod
    def jsonify_row(row):
        data = {
            "id": row.id,
            "startedAt": row.startedAt,
            "endedAt": row.endedAt,
            "elapsed": row.elapsed,
            "method": row.method,
            "args": tuple(json.loads(row.args)),  # json -> list -> tuple
            "kwargs": json.loads(row.kwargs),
            "name": row.name,
            "context": json.loads(row.context),
            "profileStats": json.loads(row.profileStats) if row.profileStats else None,
        }
        return data
    
    def begin_lockable_transaction(self, session: Session):
        return LockableTransaction(session, self.is_sqlite)
    
    def retention_deletion(self) -> bool:
        with self.Session() as session:
            # Create a transaction in order to lock on the metadata table
            with self.begin_lockable_transaction(session) as locked_transaction:
                
                try:
                    retention_period_s = float(self.config.get("retention_period_s"))
                    previous_clean_time_buffer = retention_period_s / 4
                    
                    # Lock on the metadata table to avoid multiple processes or servers duplicating effort
                    last_retention_deletion_time_sql: Select[float] = Select(Metadata.last_retention_deletion_time).with_for_update(nowait=True, of=Metadata.last_retention_deletion_time, skip_locked=True)
                    last_retention_deletion_time: ScalarResult[float] = session.execute(last_retention_deletion_time_sql).scalar_one()
                    current_time = time.time()
                    if float(last_retention_deletion_time) + previous_clean_time_buffer < current_time:
                        
                        session.execute(
                            Update(Metadata)
                            .values(last_retention_deletion_time=current_time)
                        )
                        
                        delete_sql: Delete[Measurements] = Delete(Measurements).where(Measurements.startedAt + retention_period_s < current_time)
                        session.execute(delete_sql)
                        locked_transaction.commit()
                        session.commit()
                        return True
                    return False
                except Exception as e:
                    print(e)
                    locked_transaction.rollback()
                    session.rollback()
                    return False

    def truncate(self):
        with self.Session() as session:
            try:
                session.query(Measurements).delete()
                session.query(Metadata).delete()
                session.add(Metadata(last_retention_deletion_time=0))
                session.commit()
                return True
            except Exception:
                session.rollback()
                return False

    def delete(self, measurementId):
        with self.Session() as session:
            try:
                session.query(Measurements).filter_by(id=measurementId).delete()
                session.commit()
                return True
            except Exception:
                session.rollback()
                return False

    def get(self, measurementId):
        with self.Session() as session:
            row = session.query(Measurements).filter_by(id=int(measurementId)).first()
            
            if not row:
                return {}
            
            return self.jsonify_row(row)

    def getSummary(self, kwds={}):
        filters = Sqlalchemy.getFilters(kwds)
        with self.Session() as session:
            count = func.count(Measurements.id).label('count')
            min_elapsed = func.min(Measurements.elapsed).label('minElapsed')
            max_elapsed = func.max(Measurements.elapsed).label('maxElapsed')
            avg_elapsed = func.avg(Measurements.elapsed).label('avgElapsed')
            query = session.query(
                Measurements.method,
                Measurements.name,
                count,
                min_elapsed,
                max_elapsed,
                avg_elapsed
            )

            if filters["startedAt"]:
                query = query.filter(Measurements.startedAt >= filters["startedAt"])
            if filters["endedAt"]:
                query = query.filter(Measurements.endedAt <= filters["endedAt"])
            if filters["elapsed"]:
                query = query.filter(Measurements.elapsed >= filters["elapsed"])

            query = query.group_by(Measurements.method, Measurements.name)
            if filters["sort"][1] == 'desc':
                if filters["sort"][0] == 'count':
                    query = query.order_by(count.desc())
                elif filters["sort"][0] == 'minElapsed':
                    query = query.order_by(min_elapsed.desc())
                elif filters["sort"][0] == 'maxElapsed':
                    query = query.order_by(max_elapsed.desc())
                elif filters["sort"][0] == 'avgElapsed':
                    query = query.order_by(avg_elapsed.desc())
                else:
                    query = query.order_by(
                        getattr(Measurements, filters["sort"][0]).desc())
            else:
                if filters["sort"][0] == 'count':
                    query = query.order_by(count.asc())
                elif filters["sort"][0] == 'minElapsed':
                    query = query.order_by(min_elapsed.asc())
                elif filters["sort"][0] == 'maxElapsed':
                    query = query.order_by(max_elapsed.asc())
                elif filters["sort"][0] == 'avgElapsed':
                    query = query.order_by(avg_elapsed.asc())
                else:
                    query = query.order_by(
                        getattr(Measurements, filters["sort"][0]).asc())
            
            # Add pagination support
            query = query.limit(filters["limit"]).offset(filters["skip"])
            rows = query.all()

            result = []
            for r in rows:
                result.append({
                    "method": r[0],
                    "name": r[1],
                    "count": r[2],
                    "minElapsed": r[3],
                    "maxElapsed": r[4],
                    "avgElapsed": r[5]
                })
            return result

    def getTimeseries(self, kwds={}):
        filters = Sqlalchemy.getFilters(kwds)
        if kwds.get('interval', None) == "daily":
            interval = 3600 * 24
            dateFormat = "%Y-%m-%d"
        else:
            interval = 3600
            dateFormat = "%Y-%m-%d %H"
        endedAt, startedAt = filters["endedAt"], filters["startedAt"]

        with self.Session() as session:
            rows = session.query(
                Measurements.startedAt,
            ).filter(
                Measurements.endedAt <= endedAt,
                Measurements.startedAt >= startedAt
            ).order_by(
                Measurements.startedAt.asc()
            ).all()

        rows = [datetime.fromtimestamp(float(row[0])).strftime(dateFormat) for row in rows]
        temp = set(rows)
        rows = [(t, rows.count(t)) for t in temp]
        series = {}

        for i in range(int(startedAt), int(endedAt) + 1, interval):
            series[formatDate(i, dateFormat)] = 0

        for row in rows:
            series[
                formatDate(
                    datetime.strptime(row[0], dateFormat).timestamp(),
                    dateFormat
                )
            ] = row[1]
        return series

    def getMethodDistribution(self, kwds=None):
        if not kwds:
            kwds = {}
        f = Sqlalchemy.getFilters(kwds)
        endedAt, startedAt = f["endedAt"], f["startedAt"]

        with self.Session() as session:
            rows = session.query(
                Measurements.method,
                func.count(Measurements.id)
            ).filter(
                Measurements.endedAt <= endedAt,
                Measurements.startedAt >= startedAt
            ).group_by(
                Measurements.method
            ).all()

        results = {}
        for row in rows:
            results[row[0]] = row[1]
        return results

    def close(self):
        self.db.dispose()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        
    def __del__(self):
        self.close()
