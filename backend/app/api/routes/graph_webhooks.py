"""Public Microsoft Graph mailbox notification callback."""

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.database import get_db
from app.services import email_reply_service


router = APIRouter(prefix="/microsoft-graph", tags=["Microsoft Graph webhooks"])
settings = get_settings()


@router.post("/mail-notifications", status_code=status.HTTP_202_ACCEPTED)
async def receive_mail_notifications(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Validate subscription setup or durably signal the reply worker."""

    validation_token = request.query_params.get("validationToken")
    if validation_token is not None:
        # Graph requires the decoded opaque token as plain text. Bound its size
        # so this public endpoint cannot be used as an arbitrary reflector.
        if len(validation_token) > 4096:
            return Response(status_code=status.HTTP_400_BAD_REQUEST)
        return PlainTextResponse(validation_token, status_code=status.HTTP_200_OK)
    try:
        payload = await request.json()
    except ValueError:
        # A malformed request is not a Graph notification and should not be
        # retried as though provider processing had failed.
        return Response(status_code=status.HTTP_202_ACCEPTED)
    email_reply_service.record_notifications(
        db,
        payload=payload,
        settings=settings,
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)
