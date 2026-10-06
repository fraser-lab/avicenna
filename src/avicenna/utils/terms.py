"""The term a value is recorded under, and the spellings it arrives in.

Crystallographic programs write logs, not structured output, so a value enters
avicenna spelled the way one program's log happens to spell it.  That spelling
is not the name it should be kept under: it changes between program versions,
and two programs measuring the same quantity spell it differently.  So each
sub-pipeline declares one accepted term per value and the synonyms that mean
the same thing, and everything that reads a record reads the accepted term.

Direction
---------
The table is an INBOUND normaliser: whatever a record arrives spelled as is
translated to the accepted term.  ``outbound`` exists for the one remaining
consumer that still reads the log spellings, and is meant to go away with it --
which is why the primary synonym, not the accepted term, is what it emits.

Renames only
------------
A synonym is a different NAME for the same thing.  A term that means something
DIFFERENT under the same spelling cannot be translated at all: translating it
would quietly hand a consumer a value it will misread.  Those are declared in
``DERIVED`` instead, which says where the value must come from, and the
collision check keeps them out of the synonym table.

Unknown keys pass through unchanged.  A record carries far more than the values
named here, and a vocabulary that dropped what it did not recognise would lose
whatever a newer sub-pipeline started recording.

One subclass per sub-pipeline, over this base, so a mismatch is fixed by
extending a table rather than by adding another ad-hoc dictionary somewhere.
"""


class Vocabulary(object):
    """Accepted terms and their synonyms for one sub-pipeline.

    Subclasses set ``TERMS`` and, where a spelling is a trap, ``DERIVED``.  The
    reverse index is built and checked when the subclass is defined, so a
    collision is an import-time error rather than a value silently attributed
    to the wrong term.
    """

    #: accepted term -> other spellings of the same value, most current first.
    TERMS = {}

    #: Spellings that mean something different depending on who wrote them, and
    #: where the value has to come from instead.  Never translated.
    DERIVED = {}

    _synonyms = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._synonyms = cls._build_reverse()

    @classmethod
    def _build_reverse(cls):
        """Synonym -> accepted term, refusing anything ambiguous.

        Two accepted terms claiming one synonym would make translation pick one
        of them and say nothing, which is the failure this table exists to
        prevent.
        """
        reverse = {}
        for term, synonyms in cls.TERMS.items():
            for synonym in synonyms:
                if synonym in cls.TERMS:
                    raise ValueError(
                        f"{cls.__name__}: {synonym!r} is a synonym of {term!r} "
                        f"and an accepted term in its own right"
                    )
                owner = reverse.get(synonym)
                if owner is not None and owner != term:
                    raise ValueError(
                        f"{cls.__name__}: {synonym!r} is claimed by both "
                        f"{owner!r} and {term!r}"
                    )
                reverse[synonym] = term
        for name in cls.DERIVED:
            if name in reverse or name in cls.TERMS:
                raise ValueError(
                    f"{cls.__name__}: {name!r} is declared as a derived field "
                    f"and cannot also be translated"
                )
        return reverse

    @classmethod
    def accepted(cls, name):
        """The term *name* is recorded under.  Unknown names are their own."""
        return cls._synonyms.get(name, name)

    @classmethod
    def primary(cls, term):
        """The spelling *term* goes out as at the legacy boundary."""
        synonyms = cls.TERMS.get(term)
        return synonyms[0] if synonyms else term

    @classmethod
    def inbound(cls, record):
        """*record* with every value under the term it is kept as."""
        return {cls.accepted(name): value for name, value in record.items()}

    @classmethod
    def outbound(cls, record):
        """*record* spelled the way the remaining log-fed consumers read it."""
        return {cls.primary(name): value for name, value in record.items()}


class ProcessingTerms(Vocabulary):
    """Data-processing values, as the scaling log spells them."""

    TERMS = {
        "low_res_limit": ("Low resolution limit",),
        "high_res_limit": ("High resolution limit",),
        "completeness": ("Completeness",),
        "multiplicity": ("Multiplicity",),
        "cc_half": ("Mn(I) half-set correlation CC(1/2)",),
        "rpim": ("Rpim (within I+/I-)",),
        "i_sig_i": ("Mn(I/sd(I))",),
        "unit_cell": ("Average unit cell",),
        "space_group": ("Space group",),
        "mosaicity": ("Average mosaicity",),
    }

    DERIVED = {
        # The log scraper derives it from the pin folder, a worker records the
        # full dataset tag: the same name, two different things.  The crystal's
        # identity is the crystal code, which only the project record holds, so
        # a consumer that needs it is given crystal_code explicitly.
        "dataset_code": "the pin folder to the scraper, the dataset tag to a "
        "worker -- take the crystal's identity from "
        "crystal_code in the project record instead",
    }
