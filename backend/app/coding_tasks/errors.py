class CodingFailure(ValueError):
    """A host-defined safe diagnostic; never raw native stderr or credentials."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CodingDeadline(TimeoutError):
    code = 'execution_phase_deadline'
