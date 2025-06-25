from contextlib import contextmanager
import csv
from datetime import datetime
import gzip
import hashlib
import io
import os
import tempfile
import time

import boto3
from botocore.exceptions import ClientError as BotoClientError, ConnectionError as BotoConnectionError
from jonesy import queries
import oracledb
import pytz


BATCH_SIZE = 120000
RECENT_REFRESH_CUTOFF_DAYS = 5


class Job:

    def __init__(self, name, config):
        self.name = name
        self.config = config

    def run(self):
        daily_path = get_daily_path()
        if self.name == 'upload_advisors':
            self.upload_query_results(
                queries.get_advisor_notes_access(),
                f'sis-data/sis-sysadm/{daily_path}/advisors/advisor-note-permissions/advisor-note-permissions.gz',
            )
            self.upload_query_results(
                queries.get_instructor_advisor_relationships(),
                f'sis-data/sis-sysadm/{daily_path}/advisors/instructor-advisor-map/instructor-advisor-map.gz',
            )
        elif self.name == 'upload_recent_refresh':
            recency_cutoff = datetime.fromtimestamp(time.time() - (RECENT_REFRESH_CUTOFF_DAYS * 86400))
            for term_id in self.get_current_term_ids():
                self.upload_query_results(
                    queries.get_recent_instructor_updates(term_id, recency_cutoff),
                    f'sis-data/{daily_path}/instructor_updates/instructor-updates-{term_id}.gz',
                )
                self.upload_query_results(
                    queries.get_recent_enrollment_updates(term_id, recency_cutoff),
                    f'sis-data/{daily_path}/enrollment_updates/enrollment-updates-{term_id}.gz',
                )
        elif self.name == 'upload_snapshot':
            self.upload_batched_query_results(
                queries.get_basic_attributes(),
                f'sis-data/{daily_path}/basic-attributes/basic-attributes.gz',
            )
            for term_id in self.get_current_term_ids():
                self.upload_query_results(
                    queries.get_term_courses(term_id),
                    f'sis-data/{daily_path}/courses/courses-{term_id}.gz',
                    targets='la-nessie-dev,la-nessie-qa'
                )
                self.upload_query_results(
                    queries.get_term_courses_deprecated(term_id),
                    f'sis-data/{daily_path}/courses/courses-{term_id}.gz',
                    targets='la-nessie-prod'
                )
                self.upload_batched_query_results(
                    queries.get_term_enrollments(term_id),
                    f'sis-data/{daily_path}/enrollments/enrollments-{term_id}.gz',
                )
        else:
            print(f"Job {self.name} not found, aborting")

    def get_client(self):
        session = self.get_session()
        return session.client('s3', region_name=self.config['AWS_REGION'])

    def get_current_term_ids(self):
        with sisedo_connection(self.config) as sisedo:
            term_ids = [r[0] for r in sisedo.execute(queries.get_current_terms())]
        return term_ids

    def get_session(self):
        if self.config['AWS_ROLE_ARN']:
            credentials = self.get_sts_credentials()
            return boto3.Session(
                aws_access_key_id=credentials['AccessKeyId'],
                aws_secret_access_key=credentials['SecretAccessKey'],
                aws_session_token=credentials['SessionToken'],
            )
        else:
            return boto3.Session(
                aws_access_key_id=self.config['AWS_ACCESS_KEY_ID'],
                aws_secret_access_key=self.config['AWS_SECRET_ACCESS_KEY'],
            )

    def get_sts_credentials(self):
        sts_client = boto3.client('sts')
        assumed_role_object = sts_client.assume_role(
            RoleArn=self.config['AWS_ROLE_ARN'],
            RoleSessionName='AssumeAppRoleSession',
            DurationSeconds=3600,
        )
        return assumed_role_object['Credentials']

    def upload_batched_query_results(self, batch_query, s3_key):
        with tempfile.TemporaryFile() as results_tempfile:
            results_gzipfile = gzip.GzipFile(mode='wb', fileobj=results_tempfile)
            with io.TextIOWrapper(results_gzipfile, encoding='utf-8', newline='\n') as outfile:
                with sisedo_connection(self.config) as sisedo:
                    batch = 0
                    while True:
                        sql = batch_query(batch, BATCH_SIZE)
                        row_count = _write_csv_rows(sisedo, sql, outfile)
                        # If we receive fewer rows than the batch size, we've read all available rows and are done.
                        if row_count < BATCH_SIZE:
                            break
                        batch += 1
            results_gzipfile.close()

            self.upload_data(results_tempfile, s3_key)

    def upload_data(self, data, s3_key, targets=None):
        if not targets:
            if 'TARGETS' in self.config:
                targets = self.config['TARGETS']
            else:
                print('No S3 targets specified, aborting')
                exit()
        client = self.get_client()
        for bucket in targets.split(','):
            try:
                data.seek(0)
                client.put_object(Bucket=bucket, Key=s3_key, Body=data, ServerSideEncryption='AES256')
            except (BotoClientError, BotoConnectionError, ValueError) as e:
                print(f'Error on S3 upload: bucket={bucket}, key={s3_key}, error={e}')
                return False
            print(f'S3 upload complete: bucket={bucket}, key={s3_key}')
        return True

    def upload_query_results(self, sql, s3_key, targets=None):
        with tempfile.TemporaryFile() as results_tempfile:
            results_gzipfile = gzip.GzipFile(mode='wb', fileobj=results_tempfile)
            with io.TextIOWrapper(results_gzipfile, encoding='utf-8', newline='\n') as outfile:
                with sisedo_connection(self.config) as sisedo:
                    _write_csv_rows(sisedo, sql, outfile)
            results_gzipfile.close()

            self.upload_data(results_tempfile, s3_key, targets)


def get_daily_path():
    today = datetime.now().strftime('%Y-%m-%d')
    digest = hashlib.md5(today.encode()).hexdigest()
    return f"daily/{digest}-{today}"


@contextmanager
def sisedo_connection(config):
    with oracledb.connect(
        user=config['SISEDO_UN'],
        password=config['SISEDO_PW'],
        host=config['SISEDO_HOST'],
        port=config['SISEDO_PORT'],
        sid=config['SISEDO_SID'],
    ) as connection:
        with connection.cursor() as cursor:
            yield cursor


def _write_csv_rows(connection, sql, outfile):
    def _coerce(column_name, e):
        if isinstance(e, datetime):
            # last_updated values come in with a UTC timezone, which is wrong; they should be treated as local time.
            if column_name == 'last_updated':
                return e.astimezone(pytz.timezone('America/Los_Angeles')).strftime('%Y-%m-%d %H:%M:%S %z')
            else:
                return e.strftime('%Y-%m-%d %H:%M:%S UTC')
        else:
            return e
    
    results_writer = csv.writer(outfile, lineterminator='\n')
    result = connection.execute(sql)
    column_names = [c[0].lower() for c in result.description]
    for row_count, r in enumerate(result):
        results_writer.writerow([_coerce(column_names[idx], e) for idx, e in enumerate(r)])

    try:
        return row_count + 1
    except NameError:
        return 0
