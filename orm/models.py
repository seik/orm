import typing

import sqlalchemy
import typesystem
from typesystem.schemas import SchemaMetaclass

from orm.exceptions import MultipleMatches, NoMatch
from orm.fields import ForeignKey


class ModelMetaclass(SchemaMetaclass):
    def __new__(
        cls: type, name: str, bases: typing.Sequence[type], attrs: dict
    ) -> type:
        new_model = super(ModelMetaclass, cls).__new__(  # type: ignore
            cls, name, bases, attrs
        )

        if attrs.get("__abstract__"):
            return new_model

        tablename = attrs["__tablename__"]
        metadata = attrs["__metadata__"]
        pkname = None

        columns = []
        for name, field in new_model.fields.items():
            if field.primary_key:
                pkname = name
            columns.append(field.get_column(name))

        new_model.__table__ = sqlalchemy.Table(tablename, metadata, *columns)
        new_model.__pkname__ = pkname

        return new_model


class QuerySet:
    def __init__(self, model_cls=None, filter_clauses=None, select_related=None):
        self.model_cls = model_cls
        self.filter_clauses = [] if filter_clauses is None else filter_clauses
        self._select_related = [] if select_related is None else select_related

    def __get__(self, instance, owner):
        return self.__class__(model_cls=owner)

    @property
    def database(self):
        return self.model_cls.__database__

    @property
    def table(self):
        return self.model_cls.__table__

    def build_select_expression(self):
        tables = [self.table]
        select_from = self.table
        for item in self._select_related:
            to_table = self.model_cls.fields[item].to.__table__
            tables.append(to_table)
            select_from = sqlalchemy.sql.join(select_from, to_table)

        expr = sqlalchemy.sql.select(tables)
        if len(tables) > 1:
            expr.select_from(select_from)

        if self.filter_clauses:
            if len(self.filter_clauses) == 1:
                clause = self.filter_clauses[0]
            else:
                clause = sqlalchemy.sql.and_(*self.filter_clauses)
            expr = expr.where(clause)
        return expr

    def filter(self, **kwargs):
        filter_clauses = self.filter_clauses
        for key, value in kwargs.items():
            if "__" in key:
                key, op = key.split("__")
            else:
                op = "exact"

            # Map the operation code onto SQLAlchemy's ColumnElement
            # https://docs.sqlalchemy.org/en/latest/core/sqlelement.html#sqlalchemy.sql.expression.ColumnElement
            op_attr = {
                "exact": "__eq__",
                "iexact": "ilike",
                "contains": "like",
                "icontains": "ilike",
                "in": "in_",
                "gt": "__gt__",
                "gte": "__ge__",
                "lt": "__lt__",
                "lte": "__le__",
            }[op]

            if op in ["contains", "icontains"]:
                value = "%" + value + "%"

            column = self.table.columns[key]
            clause = getattr(column, op_attr)(value)
            filter_clauses.append(clause)

        return self.__class__(
            model_cls=self.model_cls,
            filter_clauses=filter_clauses,
            select_related=self._select_related,
        )

    def select_related(self, related):
        if not isinstance(related, (list, tuple)):
            related = [related]

        related = list(self._select_related) + related
        return self.__class__(
            model_cls=self.model_cls,
            filter_clauses=self.filter_clauses,
            select_related=related,
        )

    async def all(self, **kwargs):
        if kwargs:
            return await self.filter(**kwargs).all()

        expr = self.build_select_expression()
        rows = await self.database.fetch_all(expr)
        return [self.model_cls.from_row(row, select_related=self._select_related) for row in rows]

    async def get(self, **kwargs):
        if kwargs:
            return await self.filter(**kwargs).get()

        expr = self.build_select_expression().limit(2)
        rows = await self.database.fetch_all(expr)

        if not rows:
            raise NoMatch()
        if len(rows) > 1:
            raise MultipleMatches()
        return self.model_cls.from_row(rows[0], select_related=self._select_related)

    async def create(self, **kwargs):
        # Validate the keyword arguments.
        fields = self.model_cls.fields
        required = [key for key, value in fields.items() if not value.has_default()]
        validator = typesystem.Object(
            properties=fields, required=required, additional_properties=False
        )
        kwargs = validator.validate(kwargs)

        # Build the insert expression.
        expr = self.table.insert()
        expr = expr.values(**kwargs)

        # Execute the insert, and return a new model instance.
        instance = self.model_cls(kwargs)
        instance.pk = await self.database.execute(expr)
        return instance


class Model(typesystem.Schema, metaclass=ModelMetaclass):
    __abstract__ = True

    objects = QuerySet()

    def __init__(self, *args, **kwargs):
        if "pk" in kwargs:
            kwargs[self.__pkname__] = kwargs.pop("pk")
        super().__init__(*args, **kwargs)

    @property
    def pk(self):
        return getattr(self, self.__pkname__)

    @pk.setter
    def pk(self, value):
        setattr(self, self.__pkname__, value)

    async def update(self, **kwargs):
        # Validate the keyword arguments.
        fields = {key: field for key, field in self.fields.items() if key in kwargs}
        validator = typesystem.Object(properties=fields)
        kwargs = validator.validate(kwargs)

        # Build the update expression.
        pk_column = getattr(self.__table__.c, self.__pkname__)
        expr = self.__table__.update()
        expr = expr.values(**kwargs).where(pk_column == self.pk)

        # Perform the update.
        await self.__database__.execute(expr)

        # Update the model instance.
        for key, value in kwargs.items():
            setattr(self, key, value)

    async def delete(self):
        # Build the delete expression.
        pk_column = getattr(self.__table__.c, self.__pkname__)
        expr = self.__table__.delete().where(pk_column == self.pk)

        # Perform the delete.
        await self.__database__.execute(expr)

    async def load(self):
        # Build the select expression.
        pk_column = getattr(self.__table__.c, self.__pkname__)
        expr = self.__table__.select().where(pk_column == self.pk)

        # Perform the fetch.
        row = await self.__database__.fetch_one(expr)

        # Update the instance.
        for key, value in dict(row).items():
            setattr(self, key, value)

    @classmethod
    def from_row(cls, row, select_related=[]):
        item = {}
        for related in select_related:
            item[related] = cls.fields[related].to.from_row(row)

        for column in cls.__table__.columns:
            if column.name not in select_related:
                item[column.name] = row[column]

        return cls(item)


    def __setattr__(self, key, value):
        if key in self.fields:
            #  Setting a relationship to a raw pk value should set a
            # fully-fledged relationship instance, with just the pk loaded.
            value = self.fields[key].expand_relationship(value)
        super().__setattr__(key, value)
