import pytest

from traceloom.auth.permissions import MIN_ROLE, Perm, Role, can

V, M, A, W = Role.VIEWER, Role.MEMBER, Role.ADMIN, Role.OWNER

# One row per matrix line in the spec: (actions, roles allowed).
MATRIX = [
    ([Perm.TRACES_READ, Perm.PROMPTS_READ], {V, M, A, W}),
    ([Perm.ANNOTATE, Perm.PROMPTS_WRITE, Perm.PLAYGROUND_RUN], {M, A, W}),
    ([Perm.SHARE_TOGGLE, Perm.TRACES_DELETE, Perm.COMMENT_DELETE_ANY], {A, W}),
    (
        [
            Perm.PROJECT_SETTINGS,
            Perm.API_KEYS,
            Perm.MODEL_DEFINITIONS,
            Perm.INVITES,
            Perm.ROLES,
            Perm.PROJECT_CREATE,
            Perm.PROMPT_LABELS_PROTECTED,
            Perm.PROMPTS_DELETE,
            Perm.LLM_CONNECTIONS,
            Perm.WEBHOOKS,
        ],
        {A, W},
    ),
    ([Perm.OWNER_GRANT, Perm.ORG_DELETE], {W}),
]
CELLS = [
    (perm, role, role in allowed) for perms, allowed in MATRIX for perm in perms for role in Role
]


@pytest.mark.parametrize(("perm", "role", "expected"), CELLS)
def test_cell(perm, role, expected):
    assert can(role, perm) is expected


def test_matrix_covers_every_permission():
    assert {perm for perm, _, _ in CELLS} == set(Perm) == set(MIN_ROLE)


def test_no_role_means_no_access():
    assert not any(can(None, perm) for perm in Perm)


def test_parse_role():
    assert Role.parse("Admin") is Role.ADMIN
