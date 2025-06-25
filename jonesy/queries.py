# Late withdrawals are only indicated in primary section enrollments, and do not change
# any values in secondary section enrollment rows. The CASE clause implements a
# conditional join for secondary sections.
omit_drops_and_withdrawals = """
    enroll.STDNT_ENRL_STATUS_CODE != 'D' AND
    CASE enroll.GRADING_BASIS_CODE
    WHEN 'NON' THEN (
        SELECT MIN(prim_enr.GRADE_MARK)
        FROM SISEDO.CLASSSECTIONALLV01_MVW sec
        LEFT JOIN SISEDO.ETS_ENROLLMENTV01_VW prim_enr
            ON prim_enr.CLASS_SECTION_ID = sec."primaryAssociatedSectionId"
            AND prim_enr.TERM_ID = enroll.TERM_ID
            AND prim_enr.STUDENT_ID = enroll.STUDENT_ID
            AND prim_enr.STDNT_ENRL_STATUS_CODE != 'D'
         WHERE sec."id" = enroll.CLASS_SECTION_ID
            AND sec."term-id" = enroll.TERM_ID
            AND prim_enr.STUDENT_ID IS NOT NULL
    )
    ELSE enroll.GRADE_MARK END != 'W'"""


def get_advisor_notes_access():
    return """
        SELECT
            A.USER_ID,
            A.CS_ID,
            A.PERMISSION_LIST
        FROM SYSADM.BOA_ADV_NOTES_ACCESS_VW A"""


# See http://www.oracle.com/technetwork/issue-archive/2006/06-sep/o56asktom-086197.html for explanation of
# query batching with ROWNUM.
def get_basic_attributes():
    def _get_batch_basic_attributes(batch_number, batch_size):
        mininum_row_exclusive = (batch_number * batch_size)
        maximum_row_inclusive = mininum_row_exclusive + batch_size
        return f"""
            SELECT ldap_uid, sid, first_name, last_name, email_address, affiliations, person_type, alternateid
                FROM (SELECT /*+ FIRST_ROWS(n) */ attributes.*, ROWNUM rnum
                    FROM (SELECT
                        pi.ldap_uid, pi.student_id AS sid, TRIM(pi.first_name) AS first_name, TRIM(pi.last_name) as last_name,
                        pi.email_address, pi.affiliations, pi.person_type, pi.alternateid
                        FROM SISEDO.CALCENTRAL_PERSON_INFO_VW pi
                        WHERE person_type != 'Z' AND affiliations IS NOT NULL
                        ORDER BY pi.ldap_uid
                    ) attributes
                WHERE ROWNUM <= {maximum_row_inclusive})
            WHERE rnum > {mininum_row_exclusive}"""
    return _get_batch_basic_attributes


# Get the undergraduate term in progress, plus the next two. Ripley code on the other side of the pipeline will
# validate how many of these should in fact be considered 'current.'
def get_current_terms():
    return """
        SELECT * FROM (
            SELECT DISTINCT term_id FROM SISEDO.CLC_TERMV00_VW WHERE term_id >= (
                SELECT MAX(term_id) from SISEDO.CLC_TERMV00_VW where term_id < (
                    SELECT MIN(term_id)
                    FROM SISEDO.CLC_TERMV00_VW
                    WHERE institution = 'UCB01' AND
                        acadcareer_code = 'UGRD' AND
                        term_type IS NOT NULL AND
                        term_begin_dt > CURRENT_DATE
                )
            ) ORDER BY term_id
        ) WHERE rownum <= 3"""


def get_instructor_advisor_relationships():
    return """
        SELECT DISTINCT
            I.ADVISOR_ID,
            I.CAMPUS_ID,
            I.INSTRUCTOR_ADISOR_NUMBER AS INSTRUCTOR_ADVISOR_NBR,
            I.ADVISOR_TYPE,
            I.ADVISOR_TYPE_DESCR,
            I.INSTRUCTOR_TYPE,
            I.INSTRUCTOR_TYPE_DESCR,
            I.ACADEMIC_PROGRAM,
            I.ACADEMIC_PROGRAM_DESCR,
            I.ACADEMIC_PLAN,
            I.ACADEMIC_PLAN_DESCR,
            I.ACADEMIC_SUB_PLAN,
            I.ACADEMIC_SUB_PLAN_DESCR
        FROM SYSADM.BOA_INSTRUCTOR_ADVISOR_VW I
        WHERE I.INSTITUTION = 'UCB01'
            AND I.ACADEMIC_CAREER = 'UGRD'
            AND I.EFFECTIVE_STATUS = 'A'
            AND I.EFFECTIVE_DATE = (
                SELECT MAX(I1.EFFECTIVE_DATE)
                FROM SYSADM.BOA_INSTRUCTOR_ADVISOR_VW I1
                WHERE I1.ADVISOR_ID = I.ADVISOR_ID
                AND I1.INSTRUCTOR_ADISOR_NUMBER = I.INSTRUCTOR_ADISOR_NUMBER
            )"""

def get_recent_enrollment_updates(term_id, recency_cutoff):
    return f"""
        SELECT DISTINCT
            enroll.CLASS_SECTION_ID as section_id,
            enroll.TERM_ID as term_id,
            enroll.CAMPUS_UID AS ldap_uid,
            enroll.STUDENT_ID AS sis_id,
            enroll.STDNT_ENRL_STATUS_CODE AS enroll_status,
            enroll.COURSE_CAREER AS course_career,
            enroll.LAST_UPDATED as last_updated
        FROM SISEDO.ETS_ENROLLMENTV01_VW enroll
        WHERE enroll.TERM_ID = {term_id}
        AND {omit_drops_and_withdrawals}
        AND enroll.last_updated >= to_timestamp('{recency_cutoff.strftime('%Y-%m-%d %H:%M:%S')}', 'yyyy-mm-dd hh24:mi:ss')
        ORDER BY enroll.TERM_ID,
            -- In case the number of results exceeds our processing cutoff, set priority within terms by the academic
            -- career type for the course.
            CASE
                WHEN enroll.course_career = 'UGRD' THEN 1
                WHEN enroll.course_career = 'GRAD' THEN 2
                WHEN enroll.course_career = 'LAW' THEN 3
                WHEN enroll.course_career = 'UCBX' THEN 4
                ELSE 5
            END,
            enroll.CLASS_SECTION_ID, enroll.CAMPUS_UID, enroll.last_updated DESC"""


def get_recent_instructor_updates(term_id, recency_cutoff):
    return f"""
        SELECT DISTINCT
            up.instr_id AS sis_id,
            up.term_id,
            up.class_section_id AS section_id,
            up.crse_id AS course_id,
            instr."campus-uid" AS ldap_uid,
            instr."role-code" AS role_code,
            sec."primary",
            up.last_updated
            FROM SISEDO.CLASS_INSTR_UPDATESV00_VW up
            JOIN SISEDO.ASSIGNEDINSTRUCTORV00_VW instr ON (
                instr."cs-course-id" = up.crse_id AND
                instr."term-id" = up.term_id AND
                instr."session-id" = up.session_code AND
                instr."offeringNumber" = up.crse_offer_nbr AND
                instr."number" = up.class_section
            )
            JOIN SISEDO.CLASSSECTIONALLV01_MVW sec ON (
                sec."id" = up.class_section_id AND sec."term-id" = up.term_id
            )
            WHERE up.change_type IN ('C', 'U') AND up.term_id= {term_id} AND
            up.last_updated >= to_timestamp('{recency_cutoff.strftime('%Y-%m-%d %H:%M:%S')}', 'yyyy-mm-dd hh24:mi:ss')
            ORDER BY up.term_id, up.crse_id, up.class_section_id, instr."campus-uid", up.last_updated DESC"""


def get_term_courses(term_id):
    return f"""
        SELECT DISTINCT
            TO_CHAR(CLASS_NBR) AS section_id,
            STRM AS term_id,
            SESSION_CODE AS session_id,
            SUBJECT AS dept_name,
            SUBJECT AS dept_code,
            ACAD_CAREER AS course_career_code,
            SCHEDULE_PRINT AS print_in_schedule_of_classes,
            CASE WHEN PRIMARY_FLAG = 'Y' THEN 'true' ELSE 'false' END AS primary,
            SSR_COMPONENT as instruction_format,
            TO_CHAR(CLASS_NBR_1) as primary_associated_section_id,
            TRIM(DISPLAY_NAME) AS display_name,
            CLASS_SECTION AS section_num,
            CATALOG_NBR AS catalog_id,
            regexp_replace(trim(CATALOG_NBR), '[A-Za-z]') AS catalog_root,
            REPLACE(SUBSTR(REPLACE(trim(CATALOG_NBR),regexp_replace(trim(CATALOG_NBR), '[A-Za-z]'),'|'),1,1),'|','') AS catalog_prefix,
            SUBSTR(REPLACE(trim(CATALOG_NBR),regexp_replace(trim(CATALOG_NBR), '[A-Za-z]'),'|'),instr(REPLACE(trim(CATALOG_NBR),regexp_replace(trim(CATALOG_NBR), '[A-Za-z]'),'|'),'|')+1) AS catalog_suffix,
            EFFDT AS course_updated_date,
            CRSE_ID as course_id,
            CRSE_OFFER_NBR as course_offer_nbr,
            ENRL_TOT AS enrollment_count,
            ENRL_CAP AS enroll_limit,
            WAIT_CAP AS waitlist_limit,
            START_DT AS start_date,
            END_DT AS end_date,
            CAMPUS_ID AS instructor_uid,
            TRIM(
                TRIM(NAME_PREFIX) || ' ' ||
                TRIM(FIRST_NAME) || ' ' ||
                TRIM(MIDDLE_NAME) || NVL2(TRIM(MIDDLE_NAME), ' ', '') ||
                TRIM(LAST_NAME) || ' ' ||
                TRIM(NAME_SUFFIX)
            ) AS instructor_name,
            INSTR_ROLE AS instructor_role_code,
            DESCR AS location,
            CASE WHEN MON = 'Y' THEN 'MO' END ||
                CASE WHEN TUES = 'Y' THEN 'TU' END ||
                CASE WHEN WED = 'Y' THEN 'WE' END ||
                CASE WHEN THURS = 'Y' THEN 'TH' END ||
                CASE WHEN FRI = 'Y' THEN 'FR' END ||
                CASE WHEN SAT = 'Y' THEN 'SA' END ||
                CASE WHEN SUN = 'Y' THEN 'SU' END
            AS meeting_days,
            TO_CHAR(MEETING_TIME_START,'HH24:MI') AS meeting_start_time,
            TO_CHAR(MEETING_TIME_END,'HH24:MI') AS meeting_end_time,
            START_DATE AS meeting_start_date,
            END_DATE AS meeting_end_date,
            COURSE_TITLE_LONG AS course_title,
            COURSE_TITLE AS course_title_short,
            INSTRUCTION_MODE AS instruction_mode
        FROM SISEDO.BCOURSESV00_VW
        WHERE STRM = '{term_id}'"""


def get_term_courses_deprecated(term_id):
    return f"""
        SELECT DISTINCT
            sec."id" AS section_id,
            sec."term-id" AS term_id,
            sec."session-id" AS session_id,
            crs."subjectArea" AS dept_name,
            crs."classSubjectArea" AS dept_code,
            crs."academicCareer-code" AS course_career_code,
            sec."printInScheduleOfClasses" AS print_in_schedule_of_classes,
            sec."primary" AS primary,
            sec."component-code" AS instruction_format,
            TO_CHAR(sec."primaryAssociatedSectionId") AS primary_associated_section_id,
            sec."displayName" AS section_display_name,
            sec."sectionNumber" AS section_num,
            crs."displayName" AS course_display_name,
            crs."catalogNumber-formatted" AS catalog_id,
            crs."catalogNumber-number" AS catalog_root,
            crs."catalogNumber-prefix" AS catalog_prefix,
            crs."catalogNumber-suffix" AS catalog_suffix,
            crs."updatedDate" AS course_updated_date,
            crs."cms-version-independent-id" AS course_version_independent_id,
            sec."enrolledCount" AS enrollment_count,
            sec."maxEnroll" AS enroll_limit,
            sec."maxWaitlist" AS waitlist_limit,
            sec."startDate" AS start_date,
            sec."endDate" AS end_date,
            instr."campus-uid" AS instructor_uid,
            TRIM(instr."formattedName") AS instructor_name,
            instr."role-code" AS instructor_role_code,
            mtg."location-descr" AS location,
            mtg."meetsDays" AS meeting_days,
            mtg."startTime" AS meeting_start_time,
            mtg."endTime" AS meeting_end_time,
            mtg."startDate" AS meeting_start_date,
            mtg."endDate" AS meeting_end_date,
            TRIM(crs."title") AS course_title,
            TRIM(crs."transcriptTitle") AS course_title_short,
            sec."instructionMode-code" AS instruction_mode
        FROM
            SISEDO.CLASSSECTIONALLV01_MVW sec
        JOIN SISEDO.EXTENDED_TERM_MVW term1 ON (
            term1.STRM = sec."term-id" AND
            term1.ACAD_CAREER = 'UGRD')
        LEFT OUTER JOIN SISEDO.DISPLAYNAMEXLATV01_MVW xlat ON (xlat."classDisplayName" = sec."displayName")
        LEFT OUTER JOIN SISEDO.API_COURSEV01_MVW crs ON (xlat."courseDisplayName" = crs."displayName")
        LEFT OUTER JOIN SISEDO.MEETINGV00_VW mtg ON (
            mtg."cs-course-id" = sec."cs-course-id" AND
            mtg."term-id" = sec."term-id" AND
            mtg."session-id" = sec."session-id" AND
            mtg."offeringNumber" = sec."offeringNumber" AND
            mtg."sectionNumber" = sec."sectionNumber")
        LEFT OUTER JOIN SISEDO.ASSIGNEDINSTRUCTORV00_VW instr ON (
            instr."cs-course-id" = sec."cs-course-id" AND
            instr."term-id" = sec."term-id" AND
            instr."session-id" = sec."session-id" AND
            instr."offeringNumber" = sec."offeringNumber" AND
            instr."number" = sec."sectionNumber")
        WHERE
            sec."term-id" = '{term_id}'
            AND CAST(crs."fromDate" AS DATE) <= term1.TERM_END_DT
            AND CAST(crs."toDate" AS DATE) >= term1.TERM_END_DT
            AND crs."updatedDate" = (
                SELECT MAX(crs2."updatedDate")
                FROM SISEDO.API_COURSEV01_MVW crs2, SISEDO.EXTENDED_TERM_MVW term2
                WHERE crs2."cms-version-independent-id" = crs."cms-version-independent-id"
                AND crs2."displayName" = crs."displayName"
                AND term2.ACAD_CAREER = 'UGRD'
                AND term2.STRM = sec."term-id"
                AND (
                    (
                        CAST(crs2."fromDate" AS DATE) <= term2.TERM_END_DT AND
                        CAST(crs2."toDate" AS DATE) >= term2.TERM_END_DT
                    )
                    OR CAST(crs2."updatedDate" AS DATE) = TO_DATE('1901-01-01', 'YYYY-MM-DD')
                )
            )"""


def get_term_enrollments(term_id):
    def _get_batch_term_enrollments(batch_number, batch_size):
        mininum_row_exclusive = (batch_number * batch_size)
        maximum_row_inclusive = mininum_row_exclusive + batch_size
        return f"""
            SELECT section_id, term_id, session_id, ldap_uid, sis_id, enrollment_status, waitlist_position, units,
                    grade, grade_points, grading_basis, grade_midterm, institution FROM (
                SELECT /*+ FIRST_ROWS(n) */ enrollments.*, ROWNUM rnum FROM (
                    SELECT DISTINCT
                        enroll."CLASS_SECTION_ID" AS section_id,
                        enroll."TERM_ID" AS term_id,
                        enroll."SESSION_ID" AS session_id,
                        enroll."CAMPUS_UID" AS ldap_uid,
                        enroll."STUDENT_ID" AS sis_id,
                        enroll."STDNT_ENRL_STATUS_CODE" AS enrollment_status,
                        enroll."WAITLISTPOSITION" AS waitlist_position,
                        enroll."UNITS_TAKEN" AS units,
                        enroll."GRADE_MARK" AS grade,
                        enroll."GRADE_POINTS" AS grade_points,
                        enroll."GRADING_BASIS_CODE" AS grading_basis,
                        enroll."GRADE_MARK_MID" AS grade_midterm,
                        enroll."INSTITUTION" AS institution
                    FROM SISEDO.ETS_ENROLLMENTV01_VW enroll
                    WHERE enroll."TERM_ID" = '{term_id}'
                    ORDER BY section_id, sis_id
                ) enrollments
                WHERE ROWNUM <= {maximum_row_inclusive}
            )
            WHERE rnum > {mininum_row_exclusive}"""
    return _get_batch_term_enrollments
