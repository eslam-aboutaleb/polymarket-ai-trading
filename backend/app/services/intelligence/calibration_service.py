"""Application operations for calibration reads and verdict feedback."""

from datetime import timedelta

from sqlalchemy.orm import Session

from app.models.intelligence.governance import VerdictFeedback
from app.repositories.intelligence.assessments import AssessmentRepository, FeedbackRepository
from app.utils.time import utc_now


def calibration_assessments(
    db: Session,
    *,
    window_days: int,
    scope: str,
    user_id: int,
    limit: int | None = None,
    newest_first: bool = False,
):
    cutoff = utc_now() - timedelta(days=max(1, int(window_days)))
    return AssessmentRepository(db).scoped_calibration_rows(
        cutoff=cutoff,
        scope=scope,
        user_id=user_id,
        limit=limit,
        newest_first=newest_first,
    )


def legacy_assessment_count(db: Session, *, window_days: int, scope: str, user_id: int) -> int:
    cutoff = utc_now() - timedelta(days=max(1, int(window_days)))
    return AssessmentRepository(db).count_legacy_calibration_rows(
        cutoff=cutoff,
        scope=scope,
        user_id=user_id,
    )


def save_verdict_feedback(
    db: Session,
    *,
    assessment_id: int,
    user_id: int,
    rating: int,
    comment: str | None,
) -> VerdictFeedback | None:
    assessments = AssessmentRepository(db)
    if not assessments.exists(assessment_id):
        return None

    feedback_repository = FeedbackRepository(db)
    feedback = feedback_repository.get_for_user(assessment_id, user_id)
    if feedback is not None:
        feedback.rating = rating
        feedback.comment = comment
    else:
        feedback = VerdictFeedback(
            assessment_id=assessment_id,
            user_id=user_id,
            rating=rating,
            comment=comment,
        )
        db.add(feedback)
    db.commit()
    db.refresh(feedback)
    return feedback


def delete_verdict_feedback(db: Session, assessment_id: int, user_id: int) -> int:
    deleted = FeedbackRepository(db).delete_for_user(assessment_id, user_id)
    db.commit()
    return deleted


def verdict_feedback_summary(db: Session, assessment_id: int, user_id: int):
    repository = FeedbackRepository(db)
    mine = repository.get_for_user(assessment_id, user_id)
    aggregate = repository.aggregate_for_assessment(assessment_id)
    return mine, aggregate
