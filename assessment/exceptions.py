"""领域异常到 HTTP 状态码的统一映射。"""
from rest_framework.views import exception_handler


class DomainError(Exception):
    """所有领域服务异常的基类。"""

    status_code = 400
    default_detail = "业务规则校验失败"

    def __init__(self, detail=None):
        self.detail = detail or self.default_detail
        super().__init__(self.detail)


class InvalidDecision(DomainError):
    status_code = 400
    default_detail = "候选判定参数无效"


class NoContractFound(DomainError):
    status_code = 422
    default_detail = "该位置在事件发生时刻没有生效的保洁合同，无法归属扣分"


class AmbiguousContract(DomainError):
    status_code = 409
    default_detail = "该位置在事件发生时刻存在多个生效合同，责任区间重叠，需先修正合同数据"


class AlreadyRectified(DomainError):
    status_code = 409
    default_detail = "事件已整改，重复整改回调已忽略"


class PenaltyLocked(DomainError):
    status_code = 409
    default_detail = "处罚版本已锁定，不能修改；更正只能追加新版本"


class AlreadyReviewed(DomainError):
    status_code = 409
    default_detail = "当前版本已复核锁定，请勿重复复核"


class DuplicateDedupKey(DomainError):
    status_code = 409
    default_detail = "相同去重键的事件已存在，疑似重复立案"


class PhotoAlreadyLinked(DomainError):
    status_code = 409
    default_detail = "照片已关联事件，不能重复立案"


class PackageNotActive(DomainError):
    status_code = 409
    default_detail = "只能基于当前活动的封存包（链头）创建补充/替代包"


class PackageExportFailed(DomainError):
    status_code = 409
    default_detail = "证据文件缺失或摘要校验失败，无法导出完整封存包"


class InvalidExportArchive(DomainError):
    status_code = 400
    default_detail = "封存包导出文件无效"


def api_exception_handler(exc, context):
    """把 DomainError 转成 DRF 的标准错误响应体。"""
    from rest_framework.exceptions import APIException

    if isinstance(exc, DomainError):

        class _Mapped(APIException):
            status_code = exc.status_code
            default_detail = exc.detail
            default_code = "domain_error"

        exc = _Mapped()
    return exception_handler(exc, context)
