from django.contrib.gis import admin

from assessment.models import (
    CleaningContract,
    DuplicateCandidate,
    EscalationRecord,
    EvidencePhoto,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    Rectification,
    ReviewRecord,
    RoadGrid,
    SealExportJob,
    SealPackage,
    SealVerification,
    SealedFile,
)

admin.site.register(RoadGrid, admin.GISModelAdmin)
admin.site.register([CleaningContract, EvidencePhoto, DuplicateCandidate])
admin.site.register([ProblemEvent, Rectification])
admin.site.register([PenaltyUnit, PenaltyVersion, EscalationRecord, ReviewRecord])
admin.site.register([SealPackage, SealedFile, SealVerification, SealExportJob])
