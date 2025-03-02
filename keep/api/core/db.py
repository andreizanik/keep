"""
Keep main database module.

This module contains the CRUD database functions for Keep.
"""

import hashlib
import json
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple, Union
from uuid import uuid4

import validators
from dotenv import find_dotenv, load_dotenv
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from sqlalchemy import and_, desc, func, null, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import joinedload, selectinload, subqueryload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.sql import expression
from sqlmodel import Session, col, or_, select

from keep.api.core.db_utils import create_db_engine

# This import is required to create the tables
from keep.api.models.alert import AlertStatus, IncidentDtoIn
from keep.api.models.db.action import Action
from keep.api.models.db.alert import *  # pylint: disable=unused-wildcard-import
from keep.api.models.db.dashboard import *  # pylint: disable=unused-wildcard-import
from keep.api.models.db.extraction import *  # pylint: disable=unused-wildcard-import
from keep.api.models.db.mapping import *  # pylint: disable=unused-wildcard-import
from keep.api.models.db.preset import *  # pylint: disable=unused-wildcard-import
from keep.api.models.db.provider import *  # pylint: disable=unused-wildcard-import
from keep.api.models.db.rule import *  # pylint: disable=unused-wildcard-import
from keep.api.models.db.tenant import *  # pylint: disable=unused-wildcard-import
from keep.api.models.db.topology import *  # pylint: disable=unused-wildcard-import
from keep.api.models.db.workflow import *  # pylint: disable=unused-wildcard-import

logger = logging.getLogger(__name__)


# this is a workaround for gunicorn to load the env vars
#   becuase somehow in gunicorn it doesn't load the .env file
load_dotenv(find_dotenv())


engine = create_db_engine()
SQLAlchemyInstrumentor().instrument(enable_commenter=True, engine=engine)


def get_session() -> Session:
    """
    Creates a database session.

    Yields:
        Session: A database session
    """
    from opentelemetry import trace  # pylint: disable=import-outside-toplevel

    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("get_session"):
        with Session(engine) as session:
            yield session


def get_session_sync() -> Session:
    """
    Creates a database session.

    Returns:
        Session: A database session
    """
    return Session(engine)


def create_workflow_execution(
    workflow_id: str,
    tenant_id: str,
    triggered_by: str,
    execution_number: int = 1,
    event_id: str = None,
    fingerprint: str = None,
) -> WorkflowExecution:
    with Session(engine) as session:
        try:
            if len(triggered_by) > 255:
                triggered_by = triggered_by[:255]

            workflow_execution = WorkflowExecution(
                id=str(uuid4()),
                workflow_id=workflow_id,
                tenant_id=tenant_id,
                started=datetime.now(tz=timezone.utc),
                triggered_by=triggered_by,
                execution_number=execution_number,
                status="in_progress",
            )
            session.add(workflow_execution)

            if fingerprint:
                workflow_to_alert_execution = WorkflowToAlertExecution(
                    workflow_execution_id=workflow_execution.id,
                    alert_fingerprint=fingerprint,
                    event_id=event_id,
                )
                session.add(workflow_to_alert_execution)

            session.commit()
            return workflow_execution.id
        except IntegrityError:
            # Workflow execution already exists
            logger.debug(
                f"Failed to create a new execution for workflow {workflow_id}. Constraint is met."
            )
            raise


def get_mapping_rule_by_id(tenant_id: str, rule_id: str) -> MappingRule | None:
    rule = None
    with Session(engine) as session:
        rule: MappingRule | None = (
            session.query(MappingRule)
            .filter(MappingRule.tenant_id == tenant_id)
            .filter(MappingRule.id == rule_id)
            .first()
        )
    return rule


def get_last_completed_execution(
    session: Session, workflow_id: str
) -> WorkflowExecution:
    return session.exec(
        select(WorkflowExecution)
        .where(WorkflowExecution.workflow_id == workflow_id)
        .where(
            (WorkflowExecution.status == "success")
            | (WorkflowExecution.status == "error")
            | (WorkflowExecution.status == "providers_not_configured")
        )
        .order_by(WorkflowExecution.execution_number.desc())
        .limit(1)
    ).first()


def get_workflows_that_should_run():
    with Session(engine) as session:
        logger.debug("Checking for workflows that should run")
        workflows_with_interval = (
            session.query(Workflow)
            .filter(Workflow.is_deleted == False)
            .filter(Workflow.interval != None)
            .filter(Workflow.interval > 0)
            .all()
        )
        logger.debug(f"Found {len(workflows_with_interval)} workflows with interval")
        workflows_to_run = []
        # for each workflow:
        for workflow in workflows_with_interval:
            current_time = datetime.utcnow()
            last_execution = get_last_completed_execution(session, workflow.id)
            # if there no last execution, that's the first time we run the workflow
            if not last_execution:
                try:
                    # try to get the lock
                    workflow_execution_id = create_workflow_execution(
                        workflow.id, workflow.tenant_id, "scheduler"
                    )
                    # we succeed to get the lock on this execution number :)
                    # let's run it
                    workflows_to_run.append(
                        {
                            "tenant_id": workflow.tenant_id,
                            "workflow_id": workflow.id,
                            "workflow_execution_id": workflow_execution_id,
                        }
                    )
                # some other thread/instance has already started to work on it
                except IntegrityError:
                    continue
            # else, if the last execution was more than interval seconds ago, we need to run it
            elif (
                last_execution.started + timedelta(seconds=workflow.interval)
                <= current_time
            ):
                try:
                    # try to get the lock with execution_number + 1
                    workflow_execution_id = create_workflow_execution(
                        workflow.id,
                        workflow.tenant_id,
                        "scheduler",
                        last_execution.execution_number + 1,
                    )
                    # we succeed to get the lock on this execution number :)
                    # let's run it
                    workflows_to_run.append(
                        {
                            "tenant_id": workflow.tenant_id,
                            "workflow_id": workflow.id,
                            "workflow_execution_id": workflow_execution_id,
                        }
                    )
                    # continue to the next one
                    continue
                # some other thread/instance has already started to work on it
                except IntegrityError:
                    # we need to verify the locking is still valid and not timeouted
                    session.rollback()
                    pass
                # get the ongoing execution
                ongoing_execution = session.exec(
                    select(WorkflowExecution)
                    .where(WorkflowExecution.workflow_id == workflow.id)
                    .where(
                        WorkflowExecution.execution_number
                        == last_execution.execution_number + 1
                    )
                    .limit(1)
                ).first()
                # this is a WTF exception since if this (workflow_id, execution_number) does not exist,
                # we would be able to acquire the lock
                if not ongoing_execution:
                    logger.error(
                        f"WTF: ongoing execution not found {workflow.id} {last_execution.execution_number + 1}"
                    )
                    continue
                # if this completed, error, than that's ok - the service who locked the execution is done
                elif ongoing_execution.status != "in_progress":
                    continue
                # if the ongoing execution runs more than 60 minutes, than its timeout
                elif ongoing_execution.started + timedelta(minutes=60) <= current_time:
                    ongoing_execution.status = "timeout"
                    session.commit()
                    # re-create the execution and try to get the lock
                    try:
                        workflow_execution_id = create_workflow_execution(
                            workflow.id,
                            workflow.tenant_id,
                            "scheduler",
                            ongoing_execution.execution_number + 1,
                        )
                    # some other thread/instance has already started to work on it and that's ok
                    except IntegrityError:
                        logger.debug(
                            f"Failed to create a new execution for workflow {workflow.id} [timeout]. Constraint is met."
                        )
                        continue
                    # managed to acquire the (workflow_id, execution_number) lock
                    workflows_to_run.append(
                        {
                            "tenant_id": workflow.tenant_id,
                            "workflow_id": workflow.id,
                            "workflow_execution_id": workflow_execution_id,
                        }
                    )
            else:
                logger.debug(
                    f"Workflow {workflow.id} is already running by someone else"
                )

        return workflows_to_run


def add_or_update_workflow(
    id,
    name,
    tenant_id,
    description,
    created_by,
    interval,
    workflow_raw,
    updated_by=None,
) -> Workflow:
    with Session(engine, expire_on_commit=False) as session:
        # TODO: we need to better understanad if that's the right behavior we want
        existing_workflow = (
            session.query(Workflow)
            .filter_by(name=name)
            .filter_by(tenant_id=tenant_id)
            .first()
        )

        if existing_workflow:
            # tb: no need to override the id field here because it has foreign key constraints.
            existing_workflow.tenant_id = tenant_id
            existing_workflow.description = description
            existing_workflow.updated_by = (
                updated_by or existing_workflow.updated_by
            )  # Update the updated_by field if provided
            existing_workflow.interval = interval
            existing_workflow.workflow_raw = workflow_raw
            existing_workflow.revision += 1  # Increment the revision
            existing_workflow.last_updated = datetime.now()  # Update last_updated
            existing_workflow.is_deleted = False

        else:
            # Create a new workflow
            workflow = Workflow(
                id=id,
                name=name,
                tenant_id=tenant_id,
                description=description,
                created_by=created_by,
                updated_by=updated_by,  # Set updated_by to the provided value
                interval=interval,
                workflow_raw=workflow_raw,
            )
            session.add(workflow)

        session.commit()
        return existing_workflow if existing_workflow else workflow


def get_workflow_to_alert_execution_by_workflow_execution_id(
    workflow_execution_id: str,
) -> WorkflowToAlertExecution:
    """
    Get the WorkflowToAlertExecution entry for a given workflow execution ID.

    Args:
        workflow_execution_id (str): The workflow execution ID to filter the workflow execution by.

    Returns:
        WorkflowToAlertExecution: The WorkflowToAlertExecution object.
    """
    with Session(engine) as session:
        return (
            session.query(WorkflowToAlertExecution)
            .filter_by(workflow_execution_id=workflow_execution_id)
            .first()
        )


def get_last_workflow_workflow_to_alert_executions(
    session: Session, tenant_id: str
) -> list[WorkflowToAlertExecution]:
    """
    Get the latest workflow executions for each alert fingerprint.

    Args:
        session (Session): The database session.
        tenant_id (str): The tenant_id to filter the workflow executions by.

    Returns:
        list[WorkflowToAlertExecution]: A list of WorkflowToAlertExecution objects.
    """
    # Subquery to find the max started timestamp for each alert_fingerprint
    max_started_subquery = (
        session.query(
            WorkflowToAlertExecution.alert_fingerprint,
            func.max(WorkflowExecution.started).label("max_started"),
        )
        .join(
            WorkflowExecution,
            WorkflowToAlertExecution.workflow_execution_id == WorkflowExecution.id,
        )
        .filter(WorkflowExecution.tenant_id == tenant_id)
        .filter(WorkflowExecution.started >= datetime.now() - timedelta(days=7))
        .group_by(WorkflowToAlertExecution.alert_fingerprint)
    ).subquery("max_started_subquery")

    # Query to find WorkflowToAlertExecution entries that match the max started timestamp
    latest_workflow_to_alert_executions: list[WorkflowToAlertExecution] = (
        session.query(WorkflowToAlertExecution)
        .join(
            WorkflowExecution,
            WorkflowToAlertExecution.workflow_execution_id == WorkflowExecution.id,
        )
        .join(
            max_started_subquery,
            and_(
                WorkflowToAlertExecution.alert_fingerprint
                == max_started_subquery.c.alert_fingerprint,
                WorkflowExecution.started == max_started_subquery.c.max_started,
            ),
        )
        .filter(WorkflowExecution.tenant_id == tenant_id)
        .limit(1000)
        .all()
    )
    return latest_workflow_to_alert_executions


def get_last_workflow_execution_by_workflow_id(
    tenant_id: str, workflow_id: str
) -> Optional[WorkflowExecution]:
    with Session(engine) as session:
        workflow_execution = (
            session.query(WorkflowExecution)
            .filter(WorkflowExecution.workflow_id == workflow_id)
            .filter(WorkflowExecution.tenant_id == tenant_id)
            .filter(WorkflowExecution.started >= datetime.now() - timedelta(days=7))
            .filter(WorkflowExecution.status == "success")
            .order_by(WorkflowExecution.started.desc())
            .first()
        )
    return workflow_execution


def get_workflows_with_last_execution(tenant_id: str) -> List[dict]:
    with Session(engine) as session:
        latest_execution_cte = (
            select(
                WorkflowExecution.workflow_id,
                func.max(WorkflowExecution.started).label("last_execution_time"),
            )
            .where(WorkflowExecution.tenant_id == tenant_id)
            .where(
                WorkflowExecution.started
                >= datetime.now(tz=timezone.utc) - timedelta(days=7)
            )
            .group_by(WorkflowExecution.workflow_id)
            .limit(1000)
            .cte("latest_execution_cte")
        )

        workflows_with_last_execution_query = (
            select(
                Workflow,
                latest_execution_cte.c.last_execution_time,
                WorkflowExecution.status,
            )
            .outerjoin(
                latest_execution_cte,
                Workflow.id == latest_execution_cte.c.workflow_id,
            )
            .outerjoin(
                WorkflowExecution,
                and_(
                    Workflow.id == WorkflowExecution.workflow_id,
                    WorkflowExecution.started
                    == latest_execution_cte.c.last_execution_time,
                ),
            )
            .where(Workflow.tenant_id == tenant_id)
            .where(Workflow.is_deleted == False)
        ).distinct()

        result = session.execute(workflows_with_last_execution_query).all()
    return result


def get_all_workflows(tenant_id: str) -> List[Workflow]:
    with Session(engine) as session:
        workflows = session.exec(
            select(Workflow)
            .where(Workflow.tenant_id == tenant_id)
            .where(Workflow.is_deleted == False)
        ).all()
    return workflows


def get_all_workflows_yamls(tenant_id: str) -> List[str]:
    with Session(engine) as session:
        workflows = session.exec(
            select(Workflow.workflow_raw)
            .where(Workflow.tenant_id == tenant_id)
            .where(Workflow.is_deleted == False)
        ).all()
    return workflows


def get_workflow(tenant_id: str, workflow_id: str) -> Workflow:
    with Session(engine) as session:
        # if the workflow id is uuid:
        if validators.uuid(workflow_id):
            workflow = session.exec(
                select(Workflow)
                .where(Workflow.tenant_id == tenant_id)
                .where(Workflow.id == workflow_id)
                .where(Workflow.is_deleted == False)
            ).first()
        else:
            workflow = session.exec(
                select(Workflow)
                .where(Workflow.tenant_id == tenant_id)
                .where(Workflow.name == workflow_id)
                .where(Workflow.is_deleted == False)
            ).first()
    if not workflow:
        return None
    return workflow


def get_raw_workflow(tenant_id: str, workflow_id: str) -> str:
    workflow = get_workflow(tenant_id, workflow_id)
    if not workflow:
        return None
    return workflow.workflow_raw


def get_installed_providers(tenant_id: str) -> List[Provider]:
    with Session(engine) as session:
        providers = session.exec(
            select(Provider).where(Provider.tenant_id == tenant_id)
        ).all()
    return providers


def get_consumer_providers() -> List[Provider]:
    # get all the providers that installed as consumers
    with Session(engine) as session:
        providers = session.exec(
            select(Provider).where(Provider.consumer == True)
        ).all()
    return providers


def finish_workflow_execution(tenant_id, workflow_id, execution_id, status, error):
    with Session(engine) as session:
        workflow_execution = session.exec(
            select(WorkflowExecution)
            .where(WorkflowExecution.tenant_id == tenant_id)
            .where(WorkflowExecution.workflow_id == workflow_id)
            .where(WorkflowExecution.id == execution_id)
        ).first()
        # some random number to avoid collisions
        workflow_execution.is_running = random.randint(1, 2147483647 - 1)  # max int
        workflow_execution.status = status
        # TODO: we had a bug with the error field, it was too short so some customers may fail over it.
        #   we need to fix it in the future, create a migration that increases the size of the error field
        #   and then we can remove the [:255] from here
        workflow_execution.error = error[:255] if error else None
        workflow_execution.execution_time = (
            datetime.utcnow() - workflow_execution.started
        ).total_seconds()
        # TODO: logs
        session.commit()


def get_workflow_executions(tenant_id, workflow_id, limit=50):
    with Session(engine) as session:
        workflow_executions = session.exec(
            select(
                WorkflowExecution.id,
                WorkflowExecution.workflow_id,
                WorkflowExecution.started,
                WorkflowExecution.status,
                WorkflowExecution.triggered_by,
                WorkflowExecution.execution_time,
                WorkflowExecution.error,
            )
            .where(WorkflowExecution.tenant_id == tenant_id)
            .where(WorkflowExecution.workflow_id == workflow_id)
            .where(
                WorkflowExecution.started
                >= datetime.now(tz=timezone.utc) - timedelta(days=7)
            )
            .order_by(WorkflowExecution.started.desc())
            .limit(limit)
        ).all()
    return workflow_executions


def delete_workflow(tenant_id, workflow_id):
    with Session(engine) as session:
        workflow = session.exec(
            select(Workflow)
            .where(Workflow.tenant_id == tenant_id)
            .where(Workflow.id == workflow_id)
        ).first()

        if workflow:
            workflow.is_deleted = True
            session.commit()


def get_workflow_id(tenant_id, workflow_name):
    with Session(engine) as session:
        workflow = session.exec(
            select(Workflow)
            .where(Workflow.tenant_id == tenant_id)
            .where(Workflow.name == workflow_name)
            .where(Workflow.is_deleted == False)
        ).first()

        if workflow:
            return workflow.id


def push_logs_to_db(log_entries):
    db_log_entries = [
        WorkflowExecutionLog(
            workflow_execution_id=log_entry["workflow_execution_id"],
            timestamp=datetime.strptime(log_entry["asctime"], "%Y-%m-%d %H:%M:%S,%f"),
            message=log_entry["message"][0:255],  # limit the message to 255 chars
            context=json.loads(
                json.dumps(log_entry.get("context", {}), default=str)
            ),  # workaround to serialize any object
        )
        for log_entry in log_entries
    ]

    # Add the LogEntry instances to the database session
    with Session(engine) as session:
        session.add_all(db_log_entries)
        session.commit()


def get_workflow_execution(tenant_id: str, workflow_execution_id: str):
    with Session(engine) as session:
        execution_with_logs = (
            session.query(WorkflowExecution)
            .filter(
                WorkflowExecution.id == workflow_execution_id,
                WorkflowExecution.tenant_id == tenant_id,
            )
            .options(joinedload(WorkflowExecution.logs))
            .one()
        )
    return execution_with_logs


def get_last_workflow_executions(tenant_id: str, limit=20):
    with Session(engine) as session:
        execution_with_logs = (
            session.query(WorkflowExecution)
            .filter(
                WorkflowExecution.tenant_id == tenant_id,
            )
            .order_by(desc(WorkflowExecution.started))
            .limit(limit)
            .options(joinedload(WorkflowExecution.logs))
            .all()
        )

        return execution_with_logs


def _enrich_alert(
    session,
    tenant_id,
    fingerprint,
    enrichments,
    action_type: AlertActionType,
    action_callee: str,
    action_description: str,
    force=False,
):
    """
    Enrich an alert with the provided enrichments.

    Args:
        session (Session): The database session.
        tenant_id (str): The tenant ID to filter the alert enrichments by.
        fingerprint (str): The alert fingerprint to filter the alert enrichments by.
        enrichments (dict): The enrichments to add to the alert.
        force (bool): Whether to force the enrichment to be updated. This is used to dispose enrichments if necessary.
    """
    enrichment = get_enrichment_with_session(session, tenant_id, fingerprint)
    if enrichment:
        # if force - override exisitng enrichments. being used to dispose enrichments if necessary
        if force:
            new_enrichment_data = enrichments
        else:
            new_enrichment_data = {**enrichment.enrichments, **enrichments}
        # SQLAlchemy doesn't support updating JSON fields, so we need to do it manually
        # https://github.com/sqlalchemy/sqlalchemy/discussions/8396#discussion-4308891
        stmt = (
            update(AlertEnrichment)
            .where(AlertEnrichment.id == enrichment.id)
            .values(enrichments=new_enrichment_data)
        )
        session.execute(stmt)
        # add audit event
        audit = AlertAudit(
            tenant_id=tenant_id,
            fingerprint=fingerprint,
            user_id=action_callee,
            action=action_type.value,
            description=action_description,
        )
        session.add(audit)
        session.commit()
        # Refresh the instance to get updated data from the database
        session.refresh(enrichment)
        return enrichment
    else:
        alert_enrichment = AlertEnrichment(
            tenant_id=tenant_id,
            alert_fingerprint=fingerprint,
            enrichments=enrichments,
        )
        session.add(alert_enrichment)
        # add audit event
        audit = AlertAudit(
            tenant_id=tenant_id,
            fingerprint=fingerprint,
            user_id=action_callee,
            action=action_type.value,
            description=action_description,
        )
        session.add(audit)
        session.commit()
        return alert_enrichment


def enrich_alert(
    tenant_id,
    fingerprint,
    enrichments,
    action_type: AlertActionType,
    action_callee: str,
    action_description: str,
    session=None,
    force=False,
):
    # else, the enrichment doesn't exist, create it
    if not session:
        with Session(engine) as session:
            return _enrich_alert(
                session,
                tenant_id,
                fingerprint,
                enrichments,
                action_type,
                action_callee,
                action_description,
                force=force,
            )
    return _enrich_alert(
        session,
        tenant_id,
        fingerprint,
        enrichments,
        action_type,
        action_callee,
        action_description,
        force=force,
    )


def count_alerts(
    provider_type: str,
    provider_id: str,
    ever: bool,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    tenant_id: str,
):
    with Session(engine) as session:
        if ever:
            return (
                session.query(Alert)
                .filter(
                    Alert.tenant_id == tenant_id,
                    Alert.provider_id == provider_id,
                    Alert.provider_type == provider_type,
                )
                .count()
            )
        else:
            return (
                session.query(Alert)
                .filter(
                    Alert.tenant_id == tenant_id,
                    Alert.provider_id == provider_id,
                    Alert.provider_type == provider_type,
                    Alert.timestamp >= start_time,
                    Alert.timestamp <= end_time,
                )
                .count()
            )


def get_enrichment(tenant_id, fingerprint):
    with Session(engine) as session:
        alert_enrichment = session.exec(
            select(AlertEnrichment)
            .where(AlertEnrichment.tenant_id == tenant_id)
            .where(AlertEnrichment.alert_fingerprint == fingerprint)
        ).first()
    return alert_enrichment


def get_enrichments(
    tenant_id: int, fingerprints: List[str]
) -> List[Optional[AlertEnrichment]]:
    """
    Get a list of alert enrichments for a list of fingerprints using a single DB query.

    :param tenant_id: The tenant ID to filter the alert enrichments by.
    :param fingerprints: A list of fingerprints to get the alert enrichments for.
    :return: A list of AlertEnrichment objects or None for each fingerprint.
    """
    with Session(engine) as session:
        result = session.exec(
            select(AlertEnrichment)
            .where(AlertEnrichment.tenant_id == tenant_id)
            .where(AlertEnrichment.alert_fingerprint.in_(fingerprints))
        ).all()
    return result


def get_enrichment_with_session(session, tenant_id, fingerprint):
    alert_enrichment = session.exec(
        select(AlertEnrichment)
        .where(AlertEnrichment.tenant_id == tenant_id)
        .where(AlertEnrichment.alert_fingerprint == fingerprint)
    ).first()
    return alert_enrichment


def get_alerts_with_filters(
    tenant_id, provider_id=None, filters=None, time_delta=1
) -> list[Alert]:
    with Session(engine) as session:
        # Create the query
        query = session.query(Alert)

        # Apply subqueryload to force-load the alert_enrichment relationship
        query = query.options(subqueryload(Alert.alert_enrichment))

        # Filter by tenant_id
        query = query.filter(Alert.tenant_id == tenant_id)

        # Filter by time_delta
        query = query.filter(
            Alert.timestamp
            >= datetime.now(tz=timezone.utc) - timedelta(days=time_delta)
        )

        # Ensure Alert and AlertEnrichment are joined for subsequent filters
        query = query.outerjoin(Alert.alert_enrichment)

        # Apply filters if provided
        if filters:
            for f in filters:
                filter_key, filter_value = f.get("key"), f.get("value")
                if isinstance(filter_value, bool) and filter_value is True:
                    # If the filter value is True, we want to filter by the existence of the enrichment
                    #   e.g.: all the alerts that have ticket_id
                    if session.bind.dialect.name in ["mysql", "postgresql"]:
                        query = query.filter(
                            func.json_extract(
                                AlertEnrichment.enrichments, f"$.{filter_key}"
                            )
                            != null()
                        )
                    elif session.bind.dialect.name == "sqlite":
                        query = query.filter(
                            func.json_type(
                                AlertEnrichment.enrichments, f"$.{filter_key}"
                            )
                            != null()
                        )
                elif isinstance(filter_value, (str, int)):
                    if session.bind.dialect.name in ["mysql", "postgresql"]:
                        query = query.filter(
                            func.json_unquote(
                                func.json_extract(
                                    AlertEnrichment.enrichments, f"$.{filter_key}"
                                )
                            )
                            == filter_value
                        )
                    elif session.bind.dialect.name == "sqlite":
                        query = query.filter(
                            func.json_extract(
                                AlertEnrichment.enrichments, f"$.{filter_key}"
                            )
                            == filter_value
                        )
                    else:
                        logger.warning(
                            "Unsupported dialect",
                            extra={"dialect": session.bind.dialect.name},
                        )
                else:
                    logger.warning("Unsupported filter type", extra={"filter": f})

        if provider_id:
            query = query.filter(Alert.provider_id == provider_id)

        query = query.order_by(Alert.timestamp.desc())

        query = query.limit(10000)

        # Execute the query
        alerts = query.all()

    return alerts


def get_last_alerts(
    tenant_id, provider_id=None, limit=1000, timeframe=None
) -> list[Alert]:
    """
    Get the last alert for each fingerprint along with the first time the alert was triggered.

    Args:
        tenant_id (_type_): The tenant_id to filter the alerts by.
        provider_id (_type_, optional): The provider id to filter by. Defaults to None.

    Returns:
        List[Alert]: A list of Alert objects including the first time the alert was triggered.
    """
    with Session(engine) as session:
        # Subquery that selects the max and min timestamp for each fingerprint.
        subquery = (
            session.query(
                Alert.fingerprint,
                func.max(Alert.timestamp).label("max_timestamp"),
                func.min(Alert.timestamp).label(
                    "min_timestamp"
                ),  # Include minimum timestamp
            )
            .filter(Alert.tenant_id == tenant_id)
            .group_by(Alert.fingerprint)
            .subquery()
        )
        # if timeframe is provided, filter the alerts by the timeframe
        if timeframe:
            subquery = (
                session.query(subquery)
                .filter(
                    subquery.c.max_timestamp
                    >= datetime.now(tz=timezone.utc) - timedelta(days=timeframe)
                )
                .subquery()
            )
        # Main query joins the subquery to select alerts with their first and last occurrence.
        query = (
            session.query(
                Alert,
                subquery.c.min_timestamp.label(
                    "startedAt"
                ),  # Include "startedAt" in the selected columns
            )
            .filter(Alert.tenant_id == tenant_id)
            .join(
                subquery,
                and_(
                    Alert.fingerprint == subquery.c.fingerprint,
                    Alert.timestamp == subquery.c.max_timestamp,
                ),
            )
            .options(subqueryload(Alert.alert_enrichment))
        )

        if provider_id:
            query = query.filter(Alert.provider_id == provider_id)

        if timeframe:
            query = query.filter(
                subquery.c.max_timestamp
                >= datetime.now(tz=timezone.utc) - timedelta(days=timeframe)
            )

        # Order by timestamp in descending order and limit the results
        query = query.order_by(desc(Alert.timestamp)).limit(limit)
        # Execute the query
        alerts_with_start = query.all()
        # Convert result to list of Alert objects and include "startedAt" information if needed
        alerts = []
        for alert, startedAt in alerts_with_start:
            alert.event["startedAt"] = str(startedAt)
            alert.event["event_id"] = str(alert.id)
            alerts.append(alert)

    return alerts


def get_alerts_by_fingerprint(
    tenant_id: str, fingerprint: str, limit=1, status=None
) -> List[Alert]:
    """
    Get all alerts for a given fingerprint.

    Args:
        tenant_id (str): The tenant_id to filter the alerts by.
        fingerprint (str): The fingerprint to filter the alerts by.

    Returns:
        List[Alert]: A list of Alert objects.
    """
    with Session(engine) as session:
        # Create the query
        query = session.query(Alert)

        # Apply subqueryload to force-load the alert_enrichment relationship
        query = query.options(subqueryload(Alert.alert_enrichment))

        # Filter by tenant_id
        query = query.filter(Alert.tenant_id == tenant_id)

        query = query.filter(Alert.fingerprint == fingerprint)

        query = query.order_by(Alert.timestamp.desc())

        if status:
            query = query.filter(func.json_extract(Alert.event, "$.status") == status)

        if limit:
            query = query.limit(limit)
        # Execute the query
        alerts = query.all()

    return alerts


def get_alert_by_fingerprint_and_event_id(
    tenant_id: str, fingerprint: str, event_id: str
) -> Alert:
    with Session(engine) as session:
        alert = (
            session.query(Alert)
            .filter(Alert.tenant_id == tenant_id)
            .filter(Alert.fingerprint == fingerprint)
            .filter(Alert.id == uuid.UUID(event_id))
            .first()
        )
    return alert


def get_previous_alert_by_fingerprint(tenant_id: str, fingerprint: str) -> Alert:
    # get the previous alert for a given fingerprint
    with Session(engine) as session:
        alert = (
            session.query(Alert)
            .filter(Alert.tenant_id == tenant_id)
            .filter(Alert.fingerprint == fingerprint)
            .order_by(Alert.timestamp.desc())
            .limit(2)
            .all()
        )
    if len(alert) > 1:
        return alert[1]
    else:
        # no previous alert
        return None


def get_api_key(api_key: str) -> TenantApiKey:
    with Session(engine) as session:
        api_key_hashed = hashlib.sha256(api_key.encode()).hexdigest()
        statement = select(TenantApiKey).where(TenantApiKey.key_hash == api_key_hashed)
        tenant_api_key = session.exec(statement).first()
    return tenant_api_key


def get_user_by_api_key(api_key: str):
    api_key = get_api_key(api_key)
    return api_key.created_by


# this is only for single tenant
def get_user(username, password, update_sign_in=True):
    from keep.api.core.dependencies import SINGLE_TENANT_UUID
    from keep.api.models.db.user import User

    password_hash = hashlib.sha256(password.encode()).hexdigest()
    with Session(engine, expire_on_commit=False) as session:
        user = session.exec(
            select(User)
            .where(User.tenant_id == SINGLE_TENANT_UUID)
            .where(User.username == username)
            .where(User.password_hash == password_hash)
        ).first()
        if user and update_sign_in:
            user.last_sign_in = datetime.utcnow()
            session.add(user)
            session.commit()
    return user


def get_users():
    from keep.api.core.dependencies import SINGLE_TENANT_UUID
    from keep.api.models.db.user import User

    with Session(engine) as session:
        users = session.exec(
            select(User).where(User.tenant_id == SINGLE_TENANT_UUID)
        ).all()
    return users


def delete_user(username):
    from keep.api.core.dependencies import SINGLE_TENANT_UUID
    from keep.api.models.db.user import User

    with Session(engine) as session:
        user = session.exec(
            select(User)
            .where(User.tenant_id == SINGLE_TENANT_UUID)
            .where(User.username == username)
        ).first()
        if user:
            session.delete(user)
            session.commit()


def create_user(tenant_id, username, password, role):
    from keep.api.models.db.user import User

    password_hash = hashlib.sha256(password.encode()).hexdigest()
    with Session(engine) as session:
        user = User(
            tenant_id=tenant_id,
            username=username,
            password_hash=password_hash,
            role=role,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def save_workflow_results(tenant_id, workflow_execution_id, workflow_results):
    with Session(engine) as session:
        workflow_execution = session.exec(
            select(WorkflowExecution)
            .where(WorkflowExecution.tenant_id == tenant_id)
            .where(WorkflowExecution.id == workflow_execution_id)
        ).one()

        workflow_execution.results = workflow_results
        session.commit()


def get_workflow_id_by_name(tenant_id, workflow_name):
    with Session(engine) as session:
        workflow = session.exec(
            select(Workflow)
            .where(Workflow.tenant_id == tenant_id)
            .where(Workflow.name == workflow_name)
            .where(Workflow.is_deleted == False)
        ).first()

        if workflow:
            return workflow.id


def get_previous_execution_id(tenant_id, workflow_id, workflow_execution_id):
    with Session(engine) as session:
        previous_execution = session.exec(
            select(WorkflowExecution)
            .where(WorkflowExecution.tenant_id == tenant_id)
            .where(WorkflowExecution.workflow_id == workflow_id)
            .where(WorkflowExecution.id != workflow_execution_id)
            .order_by(WorkflowExecution.started.desc())
            .limit(1)
        ).first()
        if previous_execution:
            return previous_execution
        else:
            return None


def create_rule(
    tenant_id,
    name,
    timeframe,
    definition,
    definition_cel,
    created_by,
    grouping_criteria=[],
    group_description=None,
):
    with Session(engine) as session:
        rule = Rule(
            tenant_id=tenant_id,
            name=name,
            timeframe=timeframe,
            definition=definition,
            definition_cel=definition_cel,
            created_by=created_by,
            creation_time=datetime.utcnow(),
            grouping_criteria=grouping_criteria,
            group_description=group_description,
        )
        session.add(rule)
        session.commit()
        session.refresh(rule)
        return rule


def update_rule(
    tenant_id,
    rule_id,
    name,
    timeframe,
    definition,
    definition_cel,
    updated_by,
    grouping_criteria,
):
    with Session(engine) as session:
        rule = session.exec(
            select(Rule).where(Rule.tenant_id == tenant_id).where(Rule.id == rule_id)
        ).first()

        if rule:
            rule.name = name
            rule.timeframe = timeframe
            rule.definition = definition
            rule.definition_cel = definition_cel
            rule.grouping_criteria = grouping_criteria
            rule.updated_by = updated_by
            rule.update_time = datetime.utcnow()
            session.commit()
            session.refresh(rule)
            return rule
        else:
            return None


def get_rules(tenant_id, ids=None):
    with Session(engine) as session:
        # Start building the query
        query = select(Rule).where(Rule.tenant_id == tenant_id)

        # Apply additional filters if ids are provided
        if ids is not None:
            query = query.where(Rule.id.in_(ids))

        # Execute the query
        rules = session.exec(query).all()
        return rules


def create_alert(tenant_id, provider_type, provider_id, event, fingerprint):
    with Session(engine) as session:
        alert = Alert(
            tenant_id=tenant_id,
            provider_type=provider_type,
            provider_id=provider_id,
            event=event,
            fingerprint=fingerprint,
        )
        session.add(alert)
        session.commit()
        session.refresh(alert)
        return alert


def delete_rule(tenant_id, rule_id):
    with Session(engine) as session:
        rule = session.exec(
            select(Rule).where(Rule.tenant_id == tenant_id).where(Rule.id == rule_id)
        ).first()

        if rule:
            session.delete(rule)
            session.commit()
            return True
        return False


def assign_alert_to_group(
    tenant_id, alert_id, rule_id, timeframe, group_fingerprint
) -> Group:
    # checks if group with the group critiria exists, if not it creates it
    #   and then assign the alert to the group
    with Session(engine) as session:
        group = session.exec(
            select(Group)
            .options(joinedload(Group.alerts))
            .where(Group.tenant_id == tenant_id)
            .where(Group.rule_id == rule_id)
            .where(Group.group_fingerprint == group_fingerprint)
            .order_by(Group.creation_time.desc())
        ).first()

        # if the last alert in the group is older than the timeframe, create a new group
        is_group_expired = False
        if group:
            # group has at least one alert (o/w it wouldn't created in the first place)
            is_group_expired = max(
                alert.timestamp for alert in group.alerts
            ) < datetime.utcnow() - timedelta(seconds=timeframe)

        if is_group_expired and group:
            logger.info(
                f"Group {group.id} is expired, creating a new group for rule {rule_id}"
            )
            fingerprint = group.calculate_fingerprint()
            # enrich the group with the expired flag
            enrich_alert(
                tenant_id,
                fingerprint,
                enrichments={"group_expired": True},
                action_type=AlertActionType.GENERIC_ENRICH,  # TODO: is this a live code?
                action_callee="system",
                action_description="Enriched group with group_expired flag",
            )
            logger.info(f"Enriched group {group.id} with group_expired flag")
            # change the group status to resolve so it won't spam the UI
            #   this was asked by @bhuvanesh and should be configurable in the future (how to handle status of expired groups)
            group_alert = session.exec(
                select(Alert)
                .where(Alert.fingerprint == fingerprint)
                .order_by(Alert.timestamp.desc())
            ).first()
            # this is kinda wtf but sometimes we deleted manually
            #   these from the DB since it was too big
            if not group_alert:
                logger.warning(
                    f"Group {group.id} is expired, but the alert is not found. Did it was deleted manually?"
                )
            else:
                try:
                    session.refresh(group_alert)
                    group_alert.event["status"] = AlertStatus.RESOLVED.value
                    # mark the event as modified so it will be updated in the database
                    flag_modified(group_alert, "event")
                    # commit the changes
                    session.commit()
                    logger.info(
                        f"Updated the alert {group_alert.id} to RESOLVED status"
                    )
                except StaleDataError as e:
                    logger.warning(
                        f"Failed to update the alert {group_alert.id} to RESOLVED status",
                        extra={"exception": e},
                    )
                    pass
                # some other unknown error, we want to log it and continue
                except Exception as e:
                    logger.exception(
                        f"Failed to update the alert {group_alert.id} to RESOLVED status",
                        extra={"exception": e},
                    )
                    pass

        # if there is no group with the group_fingerprint, create it
        if not group or is_group_expired:
            # Create and add a new group if it doesn't exist
            group = Group(
                tenant_id=tenant_id,
                rule_id=rule_id,
                group_fingerprint=group_fingerprint,
            )
            session.add(group)
            session.commit()
            # Re-query the group with selectinload to set up future automatic loading of alerts
            group = session.exec(
                select(Group)
                .options(joinedload(Group.alerts))
                .where(Group.id == group.id)
            ).first()

        # Create a new AlertToGroup instance and add it
        alert_group = AlertToGroup(
            tenant_id=tenant_id,
            alert_id=str(alert_id),
            group_id=str(group.id),
        )
        session.add(alert_group)
        session.commit()
        # Requery the group to get the updated alerts
        group = session.exec(
            select(Group).options(joinedload(Group.alerts)).where(Group.id == group.id)
        ).first()
    return group


def get_groups(tenant_id):
    with Session(engine) as session:
        groups = session.exec(
            select(Group)
            .options(selectinload(Group.alerts))
            .where(Group.tenant_id == tenant_id)
        ).all()
    return groups


def get_rule(tenant_id, rule_id):
    with Session(engine) as session:
        rule = session.exec(
            select(Rule).where(Rule.tenant_id == tenant_id).where(Rule.id == rule_id)
        ).first()
    return rule


def get_rule_distribution(tenant_id, minute=False):
    """Returns hits per hour for each rule, optionally breaking down by groups if the rule has 'group by', limited to the last 7 days."""
    with Session(engine) as session:
        # Get the timestamp for 7 days ago
        seven_days_ago = datetime.utcnow() - timedelta(days=1)

        # Check the dialect
        if session.bind.dialect.name == "mysql":
            time_format = "%Y-%m-%d %H:%i" if minute else "%Y-%m-%d %H"
            timestamp_format = func.date_format(AlertToGroup.timestamp, time_format)
        elif session.bind.dialect.name == "postgresql":
            time_format = "YYYY-MM-DD HH:MI" if minute else "YYYY-MM-DD HH"
            timestamp_format = func.to_char(AlertToGroup.timestamp, time_format)
        elif session.bind.dialect.name == "sqlite":
            time_format = "%Y-%m-%d %H:%M" if minute else "%Y-%m-%d %H"
            timestamp_format = func.strftime(time_format, AlertToGroup.timestamp)
        else:
            raise ValueError("Unsupported database dialect")
        # Construct the query
        query = (
            session.query(
                Rule.id.label("rule_id"),
                Rule.name.label("rule_name"),
                Group.id.label("group_id"),
                Group.group_fingerprint.label("group_fingerprint"),
                timestamp_format.label("time"),
                func.count(AlertToGroup.alert_id).label("hits"),
            )
            .join(Group, Rule.id == Group.rule_id)
            .join(AlertToGroup, Group.id == AlertToGroup.group_id)
            .filter(AlertToGroup.timestamp >= seven_days_ago)
            .filter(Rule.tenant_id == tenant_id)  # Filter by tenant_id
            .group_by(
                "rule_id", "rule_name", "group_id", "group_fingerprint", "time"
            )  # Adjusted here
            .order_by("time")
        )

        results = query.all()

        # Convert the results into a dictionary
        rule_distribution = {}
        for result in results:
            rule_id = result.rule_id
            group_fingerprint = result.group_fingerprint
            timestamp = result.time
            hits = result.hits

            if rule_id not in rule_distribution:
                rule_distribution[rule_id] = {}

            if group_fingerprint not in rule_distribution[rule_id]:
                rule_distribution[rule_id][group_fingerprint] = {}

            rule_distribution[rule_id][group_fingerprint][timestamp] = hits

        return rule_distribution


def get_all_filters(tenant_id):
    with Session(engine) as session:
        filters = session.exec(
            select(AlertDeduplicationFilter).where(
                AlertDeduplicationFilter.tenant_id == tenant_id
            )
        ).all()
    return filters


def get_last_alert_hash_by_fingerprint(tenant_id, fingerprint):
    # get the last alert for a given fingerprint
    # to check deduplication
    with Session(engine) as session:
        alert_hash = session.exec(
            select(Alert.alert_hash)
            .where(Alert.tenant_id == tenant_id)
            .where(Alert.fingerprint == fingerprint)
            .order_by(Alert.timestamp.desc())
        ).first()
    return alert_hash


def update_key_last_used(
    tenant_id: str,
    reference_id: str,
) -> str:
    """
    Updates API key last used.

    Args:
        session (Session): _description_
        tenant_id (str): _description_
        reference_id (str): _description_

    Returns:
        str: _description_
    """
    with Session(engine) as session:
        # Get API Key from database
        statement = (
            select(TenantApiKey)
            .where(TenantApiKey.reference_id == reference_id)
            .where(TenantApiKey.tenant_id == tenant_id)
        )

        tenant_api_key_entry = session.exec(statement).first()

        # Update last used
        if not tenant_api_key_entry:
            # shouldn't happen but somehow happened to specific tenant so logging it
            logger.error(
                "API key not found",
                extra={"tenant_id": tenant_id, "unique_api_key_id": unique_api_key_id},
            )
            return
        tenant_api_key_entry.last_used = datetime.utcnow()
        session.add(tenant_api_key_entry)
        session.commit()


def get_linked_providers(tenant_id: str) -> List[Tuple[str, str, datetime]]:
    with Session(engine) as session:
        providers = (
            session.query(
                Alert.provider_type,
                Alert.provider_id,
                func.max(Alert.timestamp).label("last_alert_timestamp"),
            )
            .outerjoin(Provider, Alert.provider_id == Provider.id)
            .filter(
                Alert.tenant_id == tenant_id,
                Alert.provider_type != "group",
                Provider.id
                == None,  # Filters for alerts with a provider_id not in Provider table
            )
            .group_by(Alert.provider_type, Alert.provider_id)
            .all()
        )

    return providers


def get_provider_distribution(tenant_id: str) -> dict:
    """Returns hits per hour and the last alert timestamp for each provider, limited to the last 24 hours."""
    with Session(engine) as session:
        twenty_four_hours_ago = datetime.utcnow() - timedelta(hours=24)
        time_format = "%Y-%m-%d %H"

        if session.bind.dialect.name == "mysql":
            timestamp_format = func.date_format(Alert.timestamp, time_format)
        elif session.bind.dialect.name == "postgresql":
            # PostgreSQL requires a different syntax for the timestamp format
            # cf: https://www.postgresql.org/docs/current/functions-formatting.html#FUNCTIONS-FORMATTING
            timestamp_format = func.to_char(Alert.timestamp, "YYYY-MM-DD HH")
        elif session.bind.dialect.name == "sqlite":
            timestamp_format = func.strftime(time_format, Alert.timestamp)

        # Adjusted query to include max timestamp
        query = (
            session.query(
                Alert.provider_id,
                Alert.provider_type,
                timestamp_format.label("time"),
                func.count().label("hits"),
                func.max(Alert.timestamp).label(
                    "last_alert_timestamp"
                ),  # Include max timestamp
            )
            .filter(
                Alert.tenant_id == tenant_id,
                Alert.timestamp >= twenty_four_hours_ago,
            )
            .group_by(Alert.provider_id, Alert.provider_type, "time")
            .order_by(Alert.provider_id, Alert.provider_type, "time")
        )

        results = query.all()

        provider_distribution = {}

        for provider_id, provider_type, time, hits, last_alert_timestamp in results:
            provider_key = f"{provider_id}_{provider_type}"
            last_alert_timestamp = (
                datetime.fromisoformat(last_alert_timestamp)
                if isinstance(last_alert_timestamp, str)
                else last_alert_timestamp
            )

            if provider_key not in provider_distribution:
                provider_distribution[provider_key] = {
                    "provider_id": provider_id,
                    "provider_type": provider_type,
                    "alert_last_24_hours": [
                        {"hour": i, "number": 0} for i in range(24)
                    ],
                    "last_alert_received": last_alert_timestamp,  # Initialize with the first seen timestamp
                }
            else:
                # Update the last alert timestamp if the current one is more recent
                provider_distribution[provider_key]["last_alert_received"] = max(
                    provider_distribution[provider_key]["last_alert_received"],
                    last_alert_timestamp,
                )

            time = datetime.strptime(time, time_format)
            index = int((time - twenty_four_hours_ago).total_seconds() // 3600)

            if 0 <= index < 24:
                provider_distribution[provider_key]["alert_last_24_hours"][index][
                    "number"
                ] += hits

    return provider_distribution


def get_presets(tenant_id: str, email) -> List[Dict[str, Any]]:
    with Session(engine) as session:
        statement = (
            select(Preset)
            .where(Preset.tenant_id == tenant_id)
            .where(
                or_(
                    Preset.is_private == False,
                    Preset.created_by == email,
                )
            )
        )
        presets = session.exec(statement).all()
    return presets


def get_preset_by_name(tenant_id: str, preset_name: str) -> Preset:
    with Session(engine) as session:
        preset = session.exec(
            select(Preset)
            .where(Preset.tenant_id == tenant_id)
            .where(Preset.name == preset_name)
        ).first()
    return preset


def get_all_presets(tenant_id: str) -> List[Preset]:
    with Session(engine) as session:
        presets = session.exec(
            select(Preset).where(Preset.tenant_id == tenant_id)
        ).all()
    return presets


def get_dashboards(tenant_id: str, email=None) -> List[Dict[str, Any]]:
    with Session(engine) as session:
        statement = (
            select(Dashboard)
            .where(Dashboard.tenant_id == tenant_id)
            .where(
                or_(
                    Dashboard.is_private == False,
                    Dashboard.created_by == email,
                )
            )
        )
        dashboards = session.exec(statement).all()
    return dashboards


def create_dashboard(
    tenant_id, dashboard_name, created_by, dashboard_config, is_private=False
):
    with Session(engine) as session:
        dashboard = Dashboard(
            tenant_id=tenant_id,
            dashboard_name=dashboard_name,
            dashboard_config=dashboard_config,
            created_by=created_by,
            is_private=is_private,
        )
        session.add(dashboard)
        session.commit()
        session.refresh(dashboard)
        return dashboard


def update_dashboard(
    tenant_id, dashboard_id, dashboard_name, dashboard_config, updated_by
):
    with Session(engine) as session:
        dashboard = session.exec(
            select(Dashboard)
            .where(Dashboard.tenant_id == tenant_id)
            .where(Dashboard.id == dashboard_id)
        ).first()

        if not dashboard:
            return None

        if dashboard_name:
            dashboard.dashboard_name = dashboard_name

        if dashboard_config:
            dashboard.dashboard_config = dashboard_config

        dashboard.updated_by = updated_by
        dashboard.updated_at = datetime.utcnow()
        session.commit()
        session.refresh(dashboard)
        return dashboard


def delete_dashboard(tenant_id, dashboard_id):
    with Session(engine) as session:
        dashboard = session.exec(
            select(Dashboard)
            .where(Dashboard.tenant_id == tenant_id)
            .where(Dashboard.id == dashboard_id)
        ).first()

        if dashboard:
            session.delete(dashboard)
            session.commit()
            return True
        return False


def get_all_actions(tenant_id: str) -> List[Action]:
    with Session(engine) as session:
        actions = session.exec(
            select(Action).where(Action.tenant_id == tenant_id)
        ).all()
    return actions


def get_action(tenant_id: str, action_id: str) -> Action:
    with Session(engine) as session:
        action = session.exec(
            select(Action)
            .where(Action.tenant_id == tenant_id)
            .where(Action.id == action_id)
        ).first()
    return action


def create_action(action: Action):
    with Session(engine) as session:
        session.add(action)
        session.commit()
        session.refresh(action)


def create_actions(actions: List[Action]):
    with Session(engine) as session:
        for action in actions:
            session.add(action)
        session.commit()


def delete_action(tenant_id: str, action_id: str) -> bool:
    with Session(engine) as session:
        found_action = session.exec(
            select(Action)
            .where(Action.id == action_id)
            .where(Action.tenant_id == tenant_id)
        ).first()
        if found_action:
            session.delete(found_action)
            session.commit()
            return bool(found_action)
        return False


def update_action(
    tenant_id: str, action_id: str, update_payload: Action
) -> Union[Action, None]:
    with Session(engine) as session:
        found_action = session.exec(
            select(Action)
            .where(Action.id == action_id)
            .where(Action.tenant_id == tenant_id)
        ).first()
        if found_action:
            for key, value in update_payload.dict(exclude_unset=True).items():
                if hasattr(found_action, key):
                    setattr(found_action, key, value)
            session.commit()
            session.refresh(found_action)
    return found_action


def get_tenants_configurations() -> List[Tenant]:
    with Session(engine) as session:
        try:
            tenants = session.exec(select(Tenant)).all()
        # except column configuration does not exist (new column added)
        except OperationalError as e:
            if "Unknown column" in str(e):
                logger.warning("Column configuration does not exist in the database")
                return {}
            else:
                logger.exception("Failed to get tenants configurations")
                return {}

    tenants_configurations = {}
    for tenant in tenants:
        tenants_configurations[tenant.id] = tenant.configuration or {}

    return tenants_configurations


def update_preset_options(tenant_id: str, preset_id: str, options: dict) -> Preset:
    with Session(engine) as session:
        preset = session.exec(
            select(Preset)
            .where(Preset.tenant_id == tenant_id)
            .where(Preset.id == preset_id)
        ).first()

        stmt = (
            update(Preset)
            .where(Preset.id == preset_id)
            .where(Preset.tenant_id == tenant_id)
            .values(options=options)
        )
        session.execute(stmt)
        session.commit()
        session.refresh(preset)
    return preset


def get_incident_by_id(incident_id: UUID) -> Incident:
    with Session(engine) as session:
        incident = session.exec(
            select(Incident)
            .options(selectinload(Incident.alerts))
            .where(Incident.id == incident_id)
        ).first()
    return incident


def assign_alert_to_incident(
    alert_id: UUID, incident_id: UUID, tenant_id: str
) -> AlertToIncident:
    with Session(engine) as session:
        assignment = AlertToIncident(
            alert_id=alert_id, incident_id=incident_id, tenant_id=tenant_id
        )
        session.add(assignment)
        session.commit()
        session.refresh(assignment)

    return assignment


def get_incidents(tenant_id) -> List[Incident]:
    with Session(engine) as session:
        incidents = session.exec(
            select(Incident)
            .options(selectinload(Incident.alerts))
            .where(Incident.tenant_id == tenant_id)
            .order_by(desc(Incident.creation_time))
        ).all()
    return incidents


def get_alert_audit(
    tenant_id: str, fingerprint: str, limit: int = 50
) -> List[AlertAudit]:
    with Session(engine) as session:
        audit = session.exec(
            select(AlertAudit)
            .where(AlertAudit.tenant_id == tenant_id)
            .where(AlertAudit.fingerprint == fingerprint)
            .order_by(desc(AlertAudit.timestamp))
            .limit(limit)
        ).all()
    return audit


def get_workflows_with_last_executions_v2(
    tenant_id: str, fetch_last_executions: int = 15
) -> list[dict]:
    if fetch_last_executions is not None and fetch_last_executions > 20:
        fetch_last_executions = 20

    # List first 1000 worflows and thier last executions in the last 7 days which are active)
    with Session(engine) as session:
        latest_executions_subquery = (
            select(
                WorkflowExecution.workflow_id,
                WorkflowExecution.started,
                WorkflowExecution.execution_time,
                WorkflowExecution.status,
                func.row_number()
                .over(
                    partition_by=WorkflowExecution.workflow_id,
                    order_by=desc(WorkflowExecution.started),
                )
                .label("row_num"),
            )
            .where(WorkflowExecution.tenant_id == tenant_id)
            .where(
                WorkflowExecution.started
                >= datetime.now(tz=timezone.utc) - timedelta(days=7)
            )
            .cte("latest_executions_subquery")
        )

        workflows_with_last_executions_query = (
            select(
                Workflow,
                latest_executions_subquery.c.started,
                latest_executions_subquery.c.execution_time,
                latest_executions_subquery.c.status,
            )
            .outerjoin(
                latest_executions_subquery,
                and_(
                    Workflow.id == latest_executions_subquery.c.workflow_id,
                    latest_executions_subquery.c.row_num <= fetch_last_executions,
                ),
            )
            .where(Workflow.tenant_id == tenant_id)
            .where(Workflow.is_deleted == False)
            .order_by(Workflow.id, desc(latest_executions_subquery.c.started))
            .limit(15000)
        ).distinct()

        result = session.execute(workflows_with_last_executions_query).all()

    return result


def get_last_incidents(
    tenant_id: str,
    limit: int = 25,
    offset: int = 0,
    timeframe: int = None,
    is_confirmed: bool = False,
) -> (list[Incident], int):
    """
    Get the last incidents and total amount of incidents.

    Args:
        tenant_id (str): The tenant_id to filter the incidents by.
        limit (int): Amount of objects to return
        offset (int): Current offset for
        timeframe (int|null): Return incidents only for the last <N> days
        is_confirmed (bool): Return confirmed incidents or predictions

    Returns:
        List[Incident]: A list of Incident objects.
    """
    with Session(engine) as session:
        query = (
            session.query(
                Incident,
            )
            .filter(Incident.tenant_id == tenant_id)
            .filter(Incident.is_confirmed == is_confirmed)
            .options(joinedload(Incident.alerts))
            .order_by(desc(Incident.creation_time))
        )

        if timeframe:
            query = query.filter(
                Incident.start_time
                >= datetime.now(tz=timezone.utc) - timedelta(days=timeframe)
            )

        total_count = query.count()

        # Order by timestamp in descending order and limit the results
        query = query.order_by(desc(Incident.start_time)).limit(limit).offset(offset)
        # Execute the query
        incidents = query.all()

    return incidents, total_count


def get_incident_by_id(tenant_id: str, incident_id: str) -> Optional[Incident]:
    with Session(engine) as session:
        query = session.query(
            Incident,
        ).filter(
            Incident.tenant_id == tenant_id,
            Incident.id == incident_id,
        )

    return query.first()


def create_incident_from_dto(
    tenant_id: str, incident_dto: IncidentDtoIn
) -> Optional[Incident]:
    return create_incident_from_dict(tenant_id, incident_dto.dict())


def create_incident_from_dict(
    tenant_id: str, incident_data: dict
) -> Optional[Incident]:
    is_predicted = incident_data.get("is_predicted", False)
    with Session(engine) as session:
        new_incident = Incident(
            **incident_data, tenant_id=tenant_id, is_confirmed=not is_predicted
        )
        session.add(new_incident)
        session.commit()
        session.refresh(new_incident)
        new_incident.alerts = []
    return new_incident


def update_incident_from_dto_by_id(
    tenant_id: str,
    incident_id: str,
    updated_incident_dto: IncidentDtoIn,
) -> Optional[Incident]:
    with Session(engine) as session:
        incident = session.exec(
            select(Incident)
            .where(
                Incident.tenant_id == tenant_id,
                Incident.id == incident_id,
            )
            .options(joinedload(Incident.alerts))
        ).first()

        if not incident:
            return None

        session.query(Incident).filter(
            Incident.tenant_id == tenant_id,
            Incident.id == incident_id,
        ).update(
            {
                "name": updated_incident_dto.name,
                "description": updated_incident_dto.description,
                "assignee": updated_incident_dto.assignee,
            }
        )

        session.commit()
        session.refresh(incident)

        return incident


def delete_incident_by_id(
    tenant_id: str,
    incident_id: str,
) -> bool:
    with Session(engine) as session:
        incident = (
            session.query(Incident)
            .filter(
                Incident.tenant_id == tenant_id,
                Incident.id == incident_id,
            )
            .first()
        )

        # Delete all associations with alerts:

        (
            session.query(AlertToIncident)
            .where(
                AlertToIncident.tenant_id == tenant_id,
                AlertToIncident.incident_id == incident.id,
            )
            .delete()
        )

        session.delete(incident)
        session.commit()
        return True


def get_incidents_count(
    tenant_id: str,
) -> int:
    with Session(engine) as session:
        return (
            session.query(Incident)
            .filter(
                Incident.tenant_id == tenant_id,
            )
            .count()
        )


def get_incident_alerts_by_incident_id(tenant_id: str, incident_id: str) -> List[Alert]:
    with Session(engine) as session:
        query = (
            session.query(
                Alert,
            )
            .join(AlertToIncident, AlertToIncident.alert_id == Alert.id)
            .join(Incident, AlertToIncident.incident_id == Incident.id)
            .filter(
                AlertToIncident.tenant_id == tenant_id,
                Incident.id == incident_id,
            )
        )

    return query.all()


def add_alerts_to_incident_by_incident_id(
    tenant_id: str, incident_id: str, alert_ids: List[UUID]
):
    with Session(engine) as session:
        incident = session.exec(
            select(Incident).where(
                Incident.tenant_id == tenant_id,
                Incident.id == incident_id,
            )
        ).first()

        if not incident:
            return None

        existed_alert_ids = session.exec(
            select(AlertToIncident.alert_id).where(
                AlertToIncident.tenant_id == tenant_id,
                AlertToIncident.incident_id == incident.id,
                col(AlertToIncident.alert_id).in_(alert_ids),
            )
        ).all()

        alert_to_incident_entries = [
            AlertToIncident(
                alert_id=alert_id, incident_id=incident.id, tenant_id=tenant_id
            )
            for alert_id in alert_ids
            if alert_id not in existed_alert_ids
        ]

        session.bulk_save_objects(alert_to_incident_entries)
        session.commit()
        return True


def remove_alerts_to_incident_by_incident_id(
    tenant_id: str, incident_id: str, alert_ids: List[UUID]
) -> Optional[int]:
    with Session(engine) as session:
        incident = session.exec(
            select(Incident).where(
                Incident.tenant_id == tenant_id,
                Incident.id == incident_id,
            )
        ).first()

        if not incident:
            return None

        deleted = (
            session.query(AlertToIncident)
            .where(
                AlertToIncident.tenant_id == tenant_id,
                AlertToIncident.incident_id == incident.id,
                col(AlertToIncident.alert_id).in_(alert_ids),
            )
            .delete()
        )

        session.commit()
        return deleted


def get_alerts_count(
    tenant_id: str,
) -> int:
    with Session(engine) as session:
        return (
            session.query(Alert)
            .filter(
                Alert.tenant_id == tenant_id,
            )
            .count()
        )


def get_first_alert_datetime(
    tenant_id: str,
) -> datetime | None:
    with Session(engine) as session:
        first_alert = (
            session.query(Alert)
            .filter(
                Alert.tenant_id == tenant_id,
            )
            .first()
        )
        if first_alert:
            return first_alert.timestamp


def confirm_predicted_incident_by_id(
    tenant_id: str,
    incident_id: UUID | str,
):
    with Session(engine) as session:
        incident = session.exec(
            select(Incident)
            .where(
                Incident.tenant_id == tenant_id,
                Incident.id == incident_id,
                Incident.is_confirmed == expression.false(),
            )
            .options(joinedload(Incident.alerts))
        ).first()

        if not incident:
            return None

        session.query(Incident).filter(
            Incident.tenant_id == tenant_id,
            Incident.id == incident_id,
            Incident.is_confirmed == expression.false(),
        ).update(
            {
                "is_confirmed": True,
            }
        )

        session.commit()
        session.refresh(incident)

        return incident


def get_alert_firing_time(tenant_id: str, fingerprint: str) -> timedelta:
    with Session(engine) as session:
        # Get the latest alert for this fingerprint
        latest_alert = (
            session.query(Alert)
            .filter(Alert.tenant_id == tenant_id)
            .filter(Alert.fingerprint == fingerprint)
            .order_by(Alert.timestamp.desc())
            .first()
        )

        if not latest_alert:
            return timedelta()

        # Extract status from the event column
        latest_status = latest_alert.event.get("status")

        # If the latest status is not 'firing', return 0
        if latest_status != "firing":
            return timedelta()

        # Find the last time it wasn't firing
        last_non_firing = (
            session.query(Alert)
            .filter(Alert.tenant_id == tenant_id)
            .filter(Alert.fingerprint == fingerprint)
            .filter(func.json_extract(Alert.event, "$.status") != "firing")
            .order_by(Alert.timestamp.desc())
            .first()
        )

        if last_non_firing:
            # Find the next firing alert after the last non-firing alert
            next_firing = (
                session.query(Alert)
                .filter(Alert.tenant_id == tenant_id)
                .filter(Alert.fingerprint == fingerprint)
                .filter(Alert.timestamp > last_non_firing.timestamp)
                .filter(func.json_extract(Alert.event, "$.status") == "firing")
                .order_by(Alert.timestamp.asc())
                .first()
            )
            if next_firing:
                return datetime.now(tz=timezone.utc) - next_firing.timestamp.replace(
                    tzinfo=timezone.utc
                )
            else:
                # If no firing alert after the last non-firing, return 0
                return timedelta()
        else:
            # If all alerts are firing, use the earliest alert time
            earliest_alert = (
                session.query(Alert)
                .filter(Alert.tenant_id == tenant_id)
                .filter(Alert.fingerprint == fingerprint)
                .order_by(Alert.timestamp.asc())
                .first()
            )
            return datetime.now(tz=timezone.utc) - earliest_alert.timestamp.replace(
                tzinfo=timezone.utc
            )


# Fetch all topology data
def get_all_topology_data(
    tenant_id: str,
    provider_id: Optional[str] = None,
    service: Optional[str] = None,
    environment: Optional[str] = None,
) -> List[TopologyServiceDtoOut]:
    with Session(engine) as session:
        query = select(TopologyService).where(TopologyService.tenant_id == tenant_id)

        # @tb: let's filter by service only for now and take care of it when we handle multilpe
        # services and environments and cmdbs
        # the idea is that we show the service topology regardless of the underlying provider/env
        # if provider_id is not None and service is not None and environment is not None:
        if service is not None:
            query = query.where(
                TopologyService.service == service,
                # TopologyService.source_provider_id == provider_id,
                # TopologyService.environment == environment,
            )

            service_instance = session.exec(query).first()
            if not service_instance:
                return []

            services = session.exec(
                select(TopologyServiceDependency)
                .where(
                    TopologyServiceDependency.depends_on_service_id
                    == service_instance.id
                )
                .options(joinedload(TopologyServiceDependency.service))
            ).all()
            services = [service_instance, *[service.service for service in services]]
        else:
            # Fetch services for the tenant
            services = session.exec(query).all()

        service_dtos = [TopologyServiceDtoOut.from_orm(service) for service in services]

        return service_dtos
