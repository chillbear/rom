'''
Rom - the Redis object mapper for Python

Copyright 2013-2014 Josiah Carlson

Released under the LGPL license version 2.1 and version 3 (you can choose
which you'd like to be bound under).

Documentation
=============

Updated documentation can be found: http://pythonhosted.org/rom/

What
====

Rom is a package whose purpose is to offer active-record style data modeling
within Redis from Python, similar to the semantics of Django ORM, SQLAlchemy +
Elixir, Google's Appengine datastore, and others.

Why
===

I was building a personal project, wanted to use Redis to store some of my
data, but didn't want to hack it poorly. I looked at the existing Redis object
mappers available in Python, but didn't like the features and functionality
offered.

What is available
=================

Data types:

* Strings (2.x: str/unicode, 3.3+: str), ints, floats, decimals, booleans
* datetime.datetime, datetime.date, datetime.time
* Json columns (for nested structures)
* OneToMany and ManyToOne columns (for model references)
* Non-rom ForeignModel reference support

Indexes:

* Numeric range fetches, searches, and ordering
* Full-word text search (find me entries with col X having words A and B)
* Prefix matching (can be used for prefix-based autocomplete)
* Suffix matching (can be used for suffix-based autocomplete)
* Pattern matching on string-based columns
* All indexing is available when using Redis 2.6.0 and later

Other features:

* Per-thread entity cache (to minimize round-trips, easy saving of all
  entities)
* The ability to cache query results and get the key for any other use (see:
  ``Query.cached_result()``)

Getting started
===============

1. Make sure you have Python 2.6, 2.7, or 3.3+ installed
2. Make sure that you have Andy McCurdy's Redis client library installed:
   https://github.com/andymccurdy/redis-py/ or
   https://pypi.python.org/pypi/redis
3. Make sure that you have the Python 2 and 3 compatibility library, 'six'
   installed: https://pypi.python.org/pypi/six
4. (optional) Make sure that you have the hiredis library installed for Python
5. Make sure that you have a Redis server installed and available remotely
6. Update the Redis connection settings for ``rom`` via
   ``rom.util.set_connection_settings()`` (other connection update options,
   including per-model connections, can be read about in the ``rom.util``
   documentation)::

    import redis
    from rom import util

    util.set_connection_settings(host='myhost', db=7)

.. warning:: If you forget to update the connection function, rom will attempt
 to connect to localhost:6379 .

7. Create a model::

    import rom

    # All models to be handled by rom must derived from rom.Model
    class User(rom.Model):
        email = rom.String(required=True, unique=True, suffix=True)
        salt = rom.String()
        hash = rom.String()
        created_at = rom.Float(default=time.time)

8. Create an instance of the model and save it::

    PASSES = 32768
    def gen_hash(password, salt=None):
        salt = salt or os.urandom(16)
        comp = salt + password
        out = sha256(comp).digest()
        for i in xrange(PASSES-1):
            out = sha256(out + comp).digest()
        return salt, out

    user = User(email='user@host.com')
    user.salt, user.hash = gen_hash(password)
    user.save()
    # session.commit() or session.flush() works too

9. Load and use the object later::

    user = User.get_by(email='user@host.com')
    at_gmail = User.query.endswith(email='@gmail.com').all()

Lua support
===========

From version 0.25.0 and on, rom assumes that you are using Redis version 2.6
or later, which supports server-side Lua scripting. This allows for the
support of multiple unique columns without potentially nasty race conditions
and retries. This also allows for the support of prefix, suffix, and pattern
matching on certain column types.

If you are using a version of Redis prior to 2.6, you should upgrade Redis. If
you are unable or unwilling to upgrade Redis, but you still wish to use rom,
you should call ``rom._disable_lua_writes()``, which will prevent you from
using features that require Lua scripting support.
'''

from collections import defaultdict
import copy
from datetime import datetime, date, time as dtime
from decimal import Decimal as _Decimal
import json

import redis
import six

from .columns import (Column, Integer, Boolean, Float, Decimal, DateTime,
    Date, Time, Text, Json, Point, RouteCol, PrimaryKey, ManyToOne, ForeignModel, OneToMany,
    MODELS, _on_delete, SKIP_ON_DELETE)
from .exceptions import (ORMError, UniqueKeyViolation, InvalidOperation,
    QueryError, ColumnError, MissingColumn, InvalidColumnValue, RestrictError)
from .index import GeneralIndex, Pattern, Prefix, Suffix
from .util import (ClassProperty, _connect, session, dt2ts, t2ts,
    _prefix_score, _script_load, _encode_unique_constraint)
from django.contrib.gis.geos import Point as GeoPoint

VERSION = '0.29.0'

COLUMN_TYPES = [Column, Integer, Boolean, Float, Decimal, DateTime, Date,
Time, Text, Json, PrimaryKey, ManyToOne, ForeignModel, OneToMany]

NUMERIC_TYPES = six.integer_types + (float, _Decimal, datetime, date, dtime)

MissingColumn, InvalidOperation # silence pyflakes

USE_LUA = True
def _enable_lua_writes():
    from . import columns
    from . import util
    global USE_LUA
    util.USE_LUA = columns.USE_LUA = USE_LUA = True

def _disable_lua_writes():
    from . import columns
    from . import util
    global USE_LUA
    util.USE_LUA = columns.USE_LUA = USE_LUA = False

__all__ = '''
    Model Column Integer Float Decimal Text Json PrimaryKey ManyToOne
    ForeignModel OneToMany Query session Boolean DateTime Date Time'''.split()

if six.PY2:
    from .columns import String
    COLUMN_TYPES.append(String)
    __all__.append('String')

class _ModelMetaclass(type):
    def __new__(cls, name, bases, dict):
        if name in MODELS:
            raise ORMError("Cannot have two models with the same name %s"%name)
        dict['_required'] = required = set()
        dict['_index'] = index = set()
        dict['_unique'] = unique = set()
        dict['_cunique'] = cunique = set()
        dict['_prefix'] = prefix = set()
        dict['_suffix'] = suffix = set()

        dict['_columns'] = columns = {}
        pkey = None

        # load all columns from any base classes to allow for validation
        odict = {}
        for ocls in reversed(bases):
            if hasattr(ocls, '_columns'):
                odict.update(ocls._columns)
        odict.update(dict)
        dict = odict

        if not any(isinstance(col, PrimaryKey) for col in dict.values()):
            if 'id' in dict:
                raise ColumnError("Cannot have non-primary key named 'id' when no explicit PrimaryKey() is defined")
            dict['id'] = PrimaryKey()

        composite_unique = []
        many_to_one = defaultdict(list)

        # validate all of our columns to ensure that they fulfill our
        # expectations
        for attr, col in dict.items():
            if isinstance(col, Column):
                columns[attr] = col
                if col._required:
                    required.add(attr)
                if col._index:
                    index.add(attr)
                if col._prefix:
                    if not USE_LUA:
                        raise ColumnError("Lua scripting must be enabled to support prefix indexes (%s.%s)"%(name, attr))
                    prefix.add(attr)
                if col._suffix:
                    if not USE_LUA:
                        raise ColumnError("Lua scripting must be enabled to support suffix indexes (%s.%s)"%(name, attr))
                    suffix.add(attr)
                if col._unique:
                    # We only allow one for performance when USE_LUA is False
                    if unique and not USE_LUA:
                        raise ColumnError(
                            "Only one unique column allowed, you have at least two: %s %s"%(
                            attr, unique)
                        )
                    unique.add(attr)
            if isinstance(col, PrimaryKey):
                if pkey:
                    raise ColumnError("Only one primary key column allowed, you have: %s %s"%(
                        pkey, attr)
                    )
                pkey = attr

            if isinstance(col, OneToMany) and not col._column and col._ftable in MODELS:
                # Check to make sure that the foreign ManyToOne table doesn't
                # have multiple references to this table to require an explicit
                # foreign column.
                refs = []
                for _a, _c in MODELS[col._ftable]._columns.items():
                    if isinstance(_c, ManyToOne) and _c._ftable == name:
                        refs.append(_a)
                if len(refs) > 1:
                    raise ColumnError("Missing required column argument to OneToMany definition on column %s"%(attr,))

            if isinstance(col, ManyToOne):
                many_to_one[col._ftable].append((attr, col))

            if attr == 'unique_together':
                if not USE_LUA:
                    raise ColumnError("Lua scripting must be enabled to support multi-column uniqueness constraints")
                composite_unique = col

        # verify reverse OneToMany attributes for these ManyToOne attributes if
        # created after referenced models
        for t, cols in many_to_one.items():
            if len(cols) == 1:
                continue
            if t not in MODELS:
                continue
            for _a, _c in MODELS[t]._columns.items():
                if isinstance(_c, OneToMany) and _c._ftable == name and not _c._column:
                    raise ColumnError("Foreign model OneToMany attribute %s.%s missing column argument"%(t, _a))

        # handle multi-column uniqueness constraints
        if composite_unique and isinstance(composite_unique[0], six.string_types):
            composite_unique = [composite_unique]

        seen = {}
        for comp in composite_unique:
            key = tuple(sorted(set(comp)))
            if len(key) == 1:
                raise ColumnError("Single-column unique constraint: %r should be defined via 'unique=True' on the %r column"%(
                    comp, key[0]))
            if key in seen:
                raise ColumnError("Multi-column unique constraint: %r not different than earlier constrant: %r"%(
                    comp, seen[key]))
            for col in key:
                if col not in columns:
                    raise ColumnError("Multi-column unique index %r references non-existant column %r"%(
                        comp, col))
            seen[key] = comp
            cunique.add(key)

        dict['_pkey'] = pkey

        key_prefix = dict.get('KEY_PREFIX') or name.lower()   # use better prefixes
        dict['_gindex'] = GeneralIndex(key_prefix)

        MODELS[name] = model = type.__new__(cls, name, bases, dict)
        return model

class Model(six.with_metaclass(_ModelMetaclass, object)):
    '''
    This is the base class for all models. You subclass from this base Model
    in order to create a model with columns. As an example::

        class User(Model):
            email_address = String(required=True, unique=True)
            salt = String(default='')
            hash = String(default='')
            created_at = Float(default=time.time, index=True)

    Which can then be used like::

        user = User(email_addrss='user@domain.com')
        user.save() # session.commit() or session.flush() works too
        user = User.get_by(email_address='user@domain.com')
        user = User.get(5)
        users = User.get([2, 6, 1, 7])

    To perform arbitrary queries on entities involving the indices that you
    defined (by passing ``index=True`` on column creation), you access the
    ``.query`` class property on the model::

        query = User.query
        query = query.filter(created_at=(time.time()-86400, time.time()))
        users = query.execute()

    .. note: You can perform single or chained queries against any/all columns
      that were defined with ``index=True``.

    **Composite/multi-column unique constraints**

    As of version 0.28.0 and later, rom supports the ability for you to have a
    unique constraint involving multiple columns. Individual columns can be
    defined unique by passing the 'unique=True' specifier during column
    definition as always.

    The attribute ``unique_together`` defines those groups of columns that when
    taken together must be unique for ``.save()`` to complete successfully.
    This will work almost exactly the same as Django's ``unique_together``, and
    is comparable to SQLAlchemy's ``UniqueConstraint()``.

    Usage::

        class UniquePosition(Model):
            x = Integer()
            y = Integer()

            unique_together = [
                ('x', 'y'),
            ]

    .. note: If one or more of the column values on an entity that is part of a
        unique constrant is None in Python, the unique constraint won't apply.
        This is the typical behavior of nulls in unique constraints inside both
        MySQL and Postgres.
    '''

    KEY_PREFIX = None
    track_dirty_fields = False
    db_writable_fields = None

    def __init__(self, **kwargs):
        self._new = not kwargs.pop('_loading', False)
        model = self._key_prefix()
        self._data = {}
        self._last = {}
        self._modified = False
        self._deleted = False
        self._init = False

        for attr in self._columns:
            cval = kwargs.get(attr, None)
            data = (model, attr, cval, not self._new)
            if self._new and attr == self._pkey and cval:
                raise InvalidColumnValue("Cannot pass primary key on object creation")
            setattr(self, attr, data)
            if cval != None:
                if not isinstance(cval, six.string_types):
                    cval = self._columns[attr].to_redis(cval)
                self._last[attr] = cval
        self._init = True
        self._reset_orig_data()
        session.add(self)

    def _reset_orig_data(self):
        """
        Reset _orig_data back
        """
        self._orig_data = copy.deepcopy(self._data)

    def refresh(self, force=False):
        if self._deleted:
            return
        if self._modified and not force:
            raise InvalidOperation("Cannot refresh a modified entity without passing force=True to override modified data")
        if self._new:
            raise InvalidOperation("Cannot refresh a new entity")

        conn = _connect(self)
        data = conn.hgetall(self._pk)
        if six.PY3:
            data = dict((k.decode(), v.decode()) for k, v in data.items())
        self.__init__(_loading=True, **data)

    @classmethod
    def _key_prefix(cls):
        return getattr(cls, 'KEY_PREFIX') or cls.__name__.lower()

    @property
    def _pk(self):
        return '%s:%s' % (self._key_prefix(), getattr(self, self._pkey))

    @property
    def _dirty_fields_key(self):
        return '{}:dirty'.format(self._pk)

    @classmethod
    def _apply_changes(cls, old, new, full=False, delete=False):
        use_lua = USE_LUA
        conn = _connect(cls)
        pk = old.get(cls._pkey) or new.get(cls._pkey)
        if not pk:
            raise ColumnError("Missing primary key value")

        model = cls._key_prefix()

        key = '%s:%s'%(model, pk)
        pipe = conn.pipeline(True)

        columns = cls._columns
        while 1:
            changes = 0
            keys = set()
            scores = {}
            data = {}
            unique = {}
            deleted = []
            udeleted = {}
            prefix = []
            suffix = []

            # check for unique keys
            if len(cls._unique) > 1 and not use_lua:
                raise ColumnError(
                    "Only one unique column allowed, you have: %s"%(unique,))

            if cls._cunique and not use_lua:
                raise ColumnError(
                    "Cannot use multi-column unique constraint 'unique_together' with Lua disabled")

            if not use_lua:
                for col in cls._unique:
                    ouval = old.get(col)
                    nuval = new.get(col)
                    nuvale = columns[col].to_redis(nuval) if nuval is not None else None

                    if six.PY2 and not isinstance(ouval, str):
                        ouval = columns[col].to_redis(ouval)
                    if not (nuval and (ouval != nuvale or full)):
                        # no changes to unique columns
                        continue

                    ikey = "%s:%s:uidx"%(model, col)
                    pipe.watch(ikey)
                    ival = pipe.hget(ikey, nuvale)
                    ival = ival if isinstance(ival, str) or ival is None else ival.decode()
                    if not ival or ival == str(pk):
                        pipe.multi()
                    else:
                        pipe.unwatch()
                        raise UniqueKeyViolation("Value %r for %s is not distinct"%(nuval, ikey))

            # update individual columns
            for attr in cls._columns:
                ikey = None
                if attr in cls._unique:
                    ikey = "%s:%s:uidx"%(model, attr)

                ca = columns[attr]
                roval = old.get(attr)
                oval = ca._from_redis(roval) if roval is not None else None

                nval = new.get(attr)
                rnval = ca.to_redis(nval) if nval is not None else None

                # Add/update standard index
                if ca._keygen and not delete and nval is not None and (ca._index or ca._prefix or ca._suffix):
                    generated = ca._keygen(nval)
                    if isinstance(generated, (list, tuple, set)):
                        if ca._index:
                            for k in generated:
                                keys.add('%s:%s'%(attr, k))
                        if ca._prefix:
                            for k in generated:
                                prefix.append([attr, k])
                        if ca._suffix:
                            for k in generated:
                                if six.PY2 and isinstance(k, str) and isinstance(cls._columns[attr], Text):
                                    try:
                                        suffix.append([attr, k.decode('utf-8')[::-1].encode('utf-8')])
                                    except UnicodeDecodeError:
                                        suffix.append([attr, k[::-1]])
                                else:
                                    suffix.append([attr, k[::-1]])
                    elif isinstance(generated, dict):
                        for k, v in generated.items():
                            if not k:
                                scores[attr] = v
                            else:
                                scores['%s:%s'%(attr, k)] = v
                    elif not generated:
                        pass
                    else:
                        raise ColumnError("Don't know how to turn %r into a sequence of keys"%(generated,))

                if nval == oval and not full:
                    continue

                changes += 1

                # Delete removed columns
                if nval is None and oval is not None:
                    if use_lua:
                        deleted.append(attr)
                        if ikey:
                            udeleted[attr] = roval
                    else:
                        pipe.hdel(key, attr)
                        if ikey:
                            pipe.hdel(ikey, roval)
                        # Index removal will occur by virtue of no index entry
                        # for this column.
                    continue

                # Add/update column value
                if nval is not None:
                    data[attr] = rnval

                # Add/update unique index
                if ikey:
                    if six.PY2 and not isinstance(roval, str):
                        roval = columns[attr].to_redis(roval)
                    if use_lua:
                        if oval is not None and roval != rnval:
                            udeleted[attr] = oval
                        unique[attr] = rnval
                    else:
                        if oval is not None:
                            pipe.hdel(ikey, roval)
                        pipe.hset(ikey, rnval, pk)

            # Add/update multi-column unique constraint
            for uniq in cls._cunique:
                attr = ':'.join(uniq)

                odata = [old.get(c) for c in uniq]
                ndata = [new.get(c) for c in uniq]
                ndata = [columns[c].to_redis(nv) if nv is not None else None for c, nv in zip(uniq, ndata)]

                if odata != ndata and None not in odata:
                    udeleted[attr] = _encode_unique_constraint(odata)

                if None not in ndata:
                    unique[attr] = _encode_unique_constraint(ndata)

            id_only = str(pk)
            if use_lua:
                redis_writer_lua(conn, model, id_only, unique, udeleted,
                    deleted, data, list(keys), scores, prefix, suffix, delete)
                return changes
            elif delete:
                changes += 1
                cls._gindex._unindex(conn, pipe, id_only)
                pipe.delete(key)
            else:
                if data:
                    pipe.hmset(key, data)
                cls._gindex.index(conn, id_only, keys, scores, prefix, suffix, pipe=pipe)

            try:
                pipe.execute()
            except redis.exceptions.WatchError:
                continue
            else:
                return changes

    def to_dict(self):
        '''
        Returns a copy of all data assigned to columns in this entity. Useful
        for returning items to JSON-enabled APIs. If you want to copy an
        entity, you should look at the ``.copy()`` method.
        '''
        return dict(self._data)

    def to_json(self):
        lite_dict = self._data
        print lite_dict
        for key, value in lite_dict.iteritems():
            if value is not None:
                if isinstance(self._columns[key], Point):
                    point = getattr(self, key)
                    lite_dict[key] = {'x': point.x, 'y': point.y}
                elif isinstance(self._columns[key], Decimal):
                    lite_dict[key] = str(value)
                elif isinstance(self._columns[key], ManyToOne):
                    lite_dict[key] = value.to_json()

        return lite_dict

    def save(self, full=False):
        '''
        Saves the current entity to Redis. Will only save changed data by
        default, but you can force a full save by passing ``full=True``.
        '''
        new = self.to_dict()
        ret = self._apply_changes(self._last, new, full or self._new)

        if self.track_dirty_fields and not self._new:
            self._update_dirty_fields()

        self._new = False
        # Now explicitly encode data for the _last attribute to make re-saving
        # work correctly in all cases.
        last = {}
        cols = self._columns
        for attr, data in new.items():
            last[attr] = cols[attr].to_redis(data) if data is not None else None

        self._last = last
        self._modified = False
        self._deleted = False
        self._reset_orig_data()

        cls = self.__class__
        conn = _connect(cls)

        # loops through and index all columns that should be indexed
        for attr in cls._columns:

            if cls._columns[attr]._index:
                index_key = '%s:indexed:%s' % (self._key_prefix(), attr)
                try:
                    val = getattr(self, attr)
                except:
                    # Means that this column does not exist in heavy model
                    val = None

                if val is not None:
                    if isinstance(cls._columns[attr], Text) or isinstance(cls._columns[attr], String):
                        conn.zadd(index_key, self.pk, 0)
                        # Need to add val -> list of pks
                        mappings_key = '%s:mappings' % index_key
                        pk_list = conn.hget(mappings_key, val)
                        if not pk_list:
                            conn.hset(mappings_key, val, json.dumps([self.pk]))
                        else:
                            pk_list = json.loads(pk_list)
                            if self.pk not in pk_list:
                                pk_list.append(self.pk)
                                conn.hset(mappings_key, val, json.dumps(pk_list))
                    elif isinstance(cls._columns[attr], ForeignModel) or isinstance(cls._columns[attr], ManyToOne):
                        continue
                    elif isinstance(cls._columns[attr], DateTime) or isinstance(cls._columns[attr], Date):
                        conn.zadd(index_key, self.pk, dt2ts(val))
                    else:
                        conn.zadd(index_key, self.pk, float(val))

        return ret

    def delete(self, **kwargs):
        '''
        Deletes the entity immediately. Also performs any on_delete operations
        specified as part of column definitions.
        '''
        if kwargs.get('skip_on_delete_i_really_mean_it') is not SKIP_ON_DELETE:
            _on_delete(self)

        session.forget(self)
        self._apply_changes(self._last, {}, delete=True)

        if self.track_dirty_fields:
            self._update_dirty_fields(clear=True)

        self._modified = True
        self._deleted = True

        # Go through and remove indexed columns from redis
        cls = self.__class__
        conn = _connect(cls)

        for attr in cls._columns:
            if cls._columns[attr]._index:
                val = getattr(self, attr)
                index_key = '%s:indexed:%s' % (self._key_prefix(), attr)
                if val is not None:
                    if isinstance(cls._columns[attr], Text):
                        conn.zrem(index_key, self.pk)
                        mappings_key = '%s:mappings' % index_key
                        pk_list = conn.hget(mappings_key, val)
                        if pk_list:
                            pk_list = map(int, json.loads(pk_list))
                            if self.pk in pk_list:
                                pk_list.remove(self.pk)
                                conn.hset(mappings_key, val, json.dumps(pk_list))
                    elif isinstance(cls._columns[attr], ForeignModel) or isinstance(cls._columns[attr], ManyToOne):
                        pass
                    else:
                        conn.zrem(index_key, self.pk)

    def get_dirty_fields(self):
        conn = _connect(self)
        key = self._dirty_fields_key

        return conn.smembers(key)

    def is_dirty(self):
        conn = _connect(self)
        key = self._dirty_fields_key

        return conn.scard(key) > 0

    def _get_modified_fields(self):
        """
        Get the fields that have changed on the model since loading it
        Works by comparing values, so should work for mutable JSON fields too
        Returns a dictionary of {field_name: last_value}
        """
        fields = {}
        for key, val in self._data.iteritems():
            if val != self._orig_data[key]:
                fields[key] = val

        return fields

    @property
    def _modified_field_names(self):
        return set(self._get_modified_fields().keys())

    def _update_dirty_fields(self, clear=False):
        '''
        Update a set that keeps track of the dirty fields (i.e. not persisted
        to the primary database)
        Key is something like "user:151:dirty"
        Either add to the set, or clear it (if clear=True)
        '''
        conn = _connect(self)
        key = self._dirty_fields_key

        if clear:
            conn.delete(key)
        elif self.db_writable_fields:
            dirty_fields = self._modified_field_names
            if not dirty_fields:
                return

            db_writable_fields = self.db_writable_fields

            # make sure it's a set
            if not isinstance(db_writable_fields, set):
                db_writable_fields = set(db_writable_fields)

            # set intersection
            dirty_fields = dirty_fields & db_writable_fields

            if dirty_fields:
                dirty_fields = list(dirty_fields)
                conn.sadd(key, *dirty_fields)

    def _unmark_dirty_fields(self, fields=None):
        """
        Mark some dirty fields as not dirty..
        """
        if not fields:
            return

        conn = _connect(self)
        key = self._dirty_fields_key

        conn.srem(key, *fields)

    def __eq__(self, other):
        """
        Custom equality by id (primary key)
        """
        if not isinstance(other, Model):
            return False
        return self.id == other.id

    def __ne__(self, other):
        return not self.__eq__(other)

    def copy(self):
        '''
        Creates a shallow copy of the given entity (any entities that can be
        retrieved from a OneToMany relationship will not be copied).
        '''
        x = self.to_dict()
        x.pop(self._pkey)
        return self.__class__(**x)

    @classmethod
    def get(cls, ids):
        '''
        Will fetch one or more entities of this type from the session or
        Redis.

        Used like::

            MyModel.get(5)
            MyModel.get([1, 6, 2, 4])

        Passing a list or a tuple will return multiple entities, in the same
        order that the ids were passed.
        '''
        conn = _connect(cls)
        # prepare the ids
        single = not isinstance(ids, (list, tuple))
        if single:
            ids = [ids]
        pks = ['%s:%s'%(cls._key_prefix(), id) for id in map(int, ids)]
        # get from the session, if possible
        out = list(map(session.get, pks))
        # if we couldn't get an instance from the session, load from Redis
        if None in out:
            pipe = conn.pipeline(True)
            idxs = []
            # Fetch missing data
            for i, data in enumerate(out):
                if data is None:
                    idxs.append(i)
                    pipe.hgetall(pks[i])
            # Update output list
            for i, data in zip(idxs, pipe.execute()):
                if data:
                    if six.PY3:
                        data = dict((k.decode(), v.decode()) for k, v in data.items())
                    out[i] = cls(_loading=True, **data)
            # Get rid of missing models
            out = [x for x in out if x]
        if single:
            return out[0] if out else None
        return out

    @classmethod
    def all_instances(cls):
        """
        Need to support this later
        """
        return None

    @classmethod
    def filter_by(cls, **kwargs):
        """
        filter_by
        """
        # Need to check for None case
        if kwargs is None or len(kwargs) == 0:
            return cls.all_instances()

        conn = _connect(cls)

        result = None
        for key, value in kwargs.iteritems():
            # Determine if we have a less than, greater than statement
            args = key.split('__')
            if len(args) == 2:
                # Let's assume that you don't use operations for strings/texts
                key = args[0]
                operation = args[1]

                if not cls._columns[key]._index:
                    raise Exception('Trying to get_by on a non-indexed column')

                index_key = '%s:indexed:%s' % (cls._key_prefix(), key)

                if isinstance(cls._columns[key], DateTime) or isinstance(cls._columns[key], Date):
                    str_value = repr(dt2ts(value))
                else:
                    str_value = str(value)

                if operation == 'lt':
                    # Less than operation
                    pk_str_list = conn.zrangebyscore(index_key, '-inf', '(' + str_value)
                elif operation == 'lte':
                    # Less than or equal to operation
                    pk_str_list = conn.zrangebyscore(index_key, '-inf', str_value)
                elif operation == 'gt':
                    # Greater than
                    pk_str_list = conn.zrangebyscore(index_key, '(' + str_value, '+inf')
                elif operation == 'gte':
                    # Greater than or equal to
                    pk_str_list = conn.zrangebyscore(index_key, str_value, '+inf')

                if not pk_str_list:
                    continue

                pk_list = map(int, pk_str_list)
            else:
                if not cls._columns[key]._index:
                    raise Exception('Trying to get_by on a non-indexed column')

                index_key = '%s:indexed:%s' % (cls._key_prefix(), key)
                mapping_key = '%s:mappings' % index_key
                if isinstance(cls._columns[key], Text):
                    mappings = conn.hget(mapping_key, value)
                    if mappings is None:
                        continue
                    pk_list = json.loads(mappings)
                elif isinstance(cls._columns[key], ManyToOne):
                    index_key = '%s:%s:idx' % (cls._key_prefix(), key)
                    pk_list = map(int, conn.zrangebyscore(index_key, float(value), float(value)))
                else:
                    pk_list = map(int, conn.zrangebyscore(index_key, float(value), float(value)))

            if result is None:
                result = set(pk_list)
            else:
                # result = result.intersection(set(pk_list))
                result = filter(result.__contains__, pk_list)

        inst_list = []
        if result is not None:
            for pk in result:
                inst_list.append(cls.get([pk], allow_create=True)[0])
        return inst_list

    @classmethod
    def get_by(cls, retrieve=False, **kwargs):
        """
        get_by - rewritten to only return one object.  The attribute MUST be indexed for this method
        to work
        """
        result = cls.filter_by(**kwargs)

        if result is None or len(result) == 0:
            if retrieve is True:
                obj = cls.heavy_class.objects.get(**kwargs)
                return cls.get(obj.pk)
            else:
                raise Exception('The object you are trying to get does not exist')

        elif len(result) > 1:
            raise Exception('Getting more than one object back')
        else:
            return result[0]

    @ClassProperty
    def query(cls):
        '''
        Returns a ``Query`` object that refers to this model to handle
        subsequent filtering.
        '''
        return Query(cls)

_redis_writer_lua = _script_load('''
local namespace = ARGV[1]
local id = ARGV[2]
local is_delete = cjson.decode(ARGV[11])

-- check and update unique column constraints
for i, write in ipairs({false, true}) do
    for col, value in pairs(cjson.decode(ARGV[3])) do
        local key = string.format('%s:%s:uidx', namespace, col)
        if write then
            redis.call('HSET', key, value, id)
        else
            local known = redis.call('HGET', key, value)
            if known ~= id and known ~= false then
                return col
            end
        end
    end
end

-- remove deleted unique constraints
for col, value in pairs(cjson.decode(ARGV[4])) do
    local key = string.format('%s:%s:uidx', namespace, col)
    local known = redis.call('HGET', key, value)
    if known == id then
        redis.call('HDEL', key, value)
    end
end

-- remove deleted columns
local deleted = cjson.decode(ARGV[5])
if #deleted > 0 then
    redis.call('HDEL', string.format('%s:%s', namespace, id), unpack(deleted))
end

-- update changed/added columns
local data = cjson.decode(ARGV[6])
if #data > 0 then
    redis.call('HMSET', string.format('%s:%s', namespace, id), unpack(data))
end

-- remove old index data, update util.clean_index_lua when changed
local idata = redis.call('HGET', namespace .. '::', id)
if idata then
    idata = cjson.decode(idata)
    if #idata == 2 then
        idata[3] = {}
        idata[4] = {}
    end
    for i, key in ipairs(idata[1]) do
        redis.call('SREM', string.format('%s:%s:idx', namespace, key), id)
    end
    for i, key in ipairs(idata[2]) do
        redis.call('ZREM', string.format('%s:%s:idx', namespace, key), id)
    end
    for i, data in ipairs(idata[3]) do
        local key = string.format('%s:%s:pre', namespace, data[1])
        local mem = string.format('%s\0%s', data[2], id)
        redis.call('ZREM', key, mem)
    end
    for i, data in ipairs(idata[4]) do
        local key = string.format('%s:%s:suf', namespace, data[1])
        local mem = string.format('%s\0%s', data[2], id)
        redis.call('ZREM', key, mem)
    end
end

if is_delete then
    redis.call('DEL', string.format('%s:%s', namespace, id))
    redis.call('HDEL', namespace .. '::', id)
end

-- add new key index data

-- add new scored index data
local nscored = {}
for key, score in pairs(cjson.decode(ARGV[8])) do
    redis.call('ZADD', string.format('%s:%s:idx', namespace, key), score, id)
    nscored[#nscored + 1] = key
end

-- add new prefix data
local nprefix = {}
for i, data in ipairs(cjson.decode(ARGV[9])) do
    local key = string.format('%s:%s:pre', namespace, data[1])
    local mem = string.format("%s\0%s", data[2], id)
    redis.call('ZADD', key, data[3], mem)
    nprefix[#nprefix + 1] = {data[1], data[2]}
end

-- add new suffix data
local nsuffix = {}
for i, data in ipairs(cjson.decode(ARGV[10])) do
    local key = string.format('%s:%s:suf', namespace, data[1])
    local mem = string.format("%s\0%s", data[2], id)
    redis.call('ZADD', key, data[3], mem)
    nsuffix[#nsuffix + 1] = {data[1], data[2]}
end

return #nscored + #nprefix + #nsuffix
''')

def redis_writer_lua(conn, namespace, id, unique, udelete, delete, data, keys,
                     scored, prefix, suffix, is_delete):
    ldata = []
    for pair in data.items():
        ldata.extend(pair)

    for item in prefix:
        item.append(_prefix_score(item[-1]))
    for item in suffix:
        item.append(_prefix_score(item[-1]))

    result = _redis_writer_lua(conn, [], [namespace, id] + list(map(json.dumps, [
        unique, udelete, delete, ldata, keys, scored, prefix, suffix, is_delete])))
    if isinstance(result, six.binary_type):
        result = result.decode()
        raise UniqueKeyViolation("Value %r for %s:%s:uidx not distinct"%(unique[result], namespace, result))

class Query(object):
    '''
    This is a query object. It behaves a lot like other query objects. Every
    operation performed on Query objects returns a new Query object. The old
    Query object *does not* have any updated filters.
    '''
    __slots__ = '_model _filters _order_by _limit'.split()
    def __init__(self, model, filters=(), order_by=None, limit=None):
        self._model = model
        self._filters = filters
        self._order_by = order_by
        self._limit = limit

    def replace(self, **kwargs):
        '''
        Copy the Query object, optionally replacing the filters, order_by, or
        limit information on the copy.
        '''
        data = {
            'model': self._model,
            'filters': self._filters,
            'order_by': self._order_by,
            'limit': self._limit,
        }
        data.update(**kwargs)
        return Query(**data)

    def filter(self, **kwargs):
        '''
        Filters should be of the form::

            # for numeric ranges, use None for open-ended ranges
            attribute=(min, max)

            # you can also query for equality by passing a single number
            attribute=value

            # for string searches, passing a plain string will require that
            # string to be in the index as a literal
            attribute=string

            # to perform an 'or' query on strings, you can pass a list of
            # strings
            attribute=[string1, string2]

        As an example, the following will return entities that have both
        ``hello`` and ``world`` in the ``String`` column ``scol`` and has a
        ``Numeric`` column ``ncol`` with value between 2 and 10 (including the
        endpoints)::

            results = MyModel.query \\
                .filter(scol='hello') \\
                .filter(scol='world') \\
                .filter(ncol=(2, 10)) \\
                .all()

        If you only want to match a single value as part of your range query,
        you can pass an integer, float, or Decimal object by itself, similar
        to the ``Model.get_by()`` method::

            results = MyModel.query \\
                .filter(ncol=5) \\
                .execute()

        .. note: Trying to use a range query `attribute=(min, max)` on string
            columns won't return any results.

        '''
        cur_filters = list(self._filters)
        for attr, value in kwargs.items():
            if isinstance(value, bool):
                value = str(bool(value))

            if isinstance(value, NUMERIC_TYPES):
                # for simple numeric equiality filters
                value = (value, value)

            if isinstance(value, six.string_types):
                cur_filters.append('%s:%s'%(attr, value))

            elif isinstance(value, tuple):
                if len(value) != 2:
                    raise QueryError("Numeric ranges require 2 endpoints, you provided %s with %r"%(len(value), value))

                tt = []
                for v in value:
                    if isinstance(v, date):
                        v = dt2ts(v)

                    if isinstance(v, dtime):
                        v = t2ts(v)
                    tt.append(v)

                value = tt

                cur_filters.append((attr, value[0], value[1]))

            elif isinstance(value, list) and value:
                cur_filters.append(['%s:%s'%(attr, v) for v in value])

            else:
                raise QueryError("Sorry, we don't know how to filter %r by %r"%(attr, value))
        return self.replace(filters=tuple(cur_filters))

    def startswith(self, **kwargs):
        '''
        When provided with keyword arguments of the form ``col=prefix``, this
        will limit the entities returned to those that have a word with the
        provided prefix in the specified column(s). This requires that the
        ``prefix=True`` option was provided during column definition.

        Usage::

            User.query.startswith(email='user@').execute()

        '''
        new = []
        for k, v in kwargs.items():
            new.append(Prefix(k, v))
        return self.replace(filters=self._filters+tuple(new))

    def endswith(self, **kwargs):
        '''
        When provided with keyword arguments of the form ``col=suffix``, this
        will limit the entities returned to those that have a word with the
        provided suffix in the specified column(s). This requires that the
        ``suffix=True`` option was provided during column definition.

        Usage::

            User.query.endswith(email='@gmail.com').execute()

        '''
        new = []
        for k, v in kwargs.items():
            new.append(Suffix(k, v[::-1]))
        return self.replace(filters=self._filters+tuple(new))

    def like(self, **kwargs):
        '''
        When provided with keyword arguments of the form ``col=pattern``, this
        will limit the entities returned to those that include the provided
        pattern. Note that 'like' queries require that the ``prefix=True``
        option must have been provided as part of the column definition.

        Patterns allow for 4 wildcard characters, whose semantics are as
        follows:

            * *?* - will match 0 or 1 of any character
            * *\** - will match 0 or more of any character
            * *+* - will match 1 or more of any character
            * *!* - will match exactly 1 of any character

        As an example, imagine that you have enabled the required prefix
        matching on your ``User.email`` column. And lets say that you want to
        find everyone with an email address that contains the name 'frank'
        before the ``@`` sign. You can use either of the following patterns
        to discover those users.

            * *\*frank\*@*
            * *\*frank\*@

        .. note: Like queries implicitly start at the beginning of strings
          checked, so if you want to match a pattern that doesn't start at
          the beginning of a string, you should prefix it with one of the
          wildcard characters (like ``*`` as we did with the 'frank' pattern).
        '''
        new = []
        for k, v in kwargs.items():
            new.append(Pattern(k, v))
        return self.replace(filters=self._filters+tuple(new))

    def order_by(self, column):
        '''
        When provided with a column name, will sort the results of your query::

            # returns all users, ordered by the created_at column in
            # descending order
            User.query.order_by('-created_at').execute()
        '''
        return self.replace(order_by=column)

    def limit(self, offset, count):
        '''
        Will limit the number of results returned from a query::

            # returns the most recent 25 users
            User.query.order_by('-created_at').limit(0, 25).execute()
        '''
        return self.replace(limit=(offset, count))

    def count(self):
        '''
        Will return the total count of the objects that match the specified
        filters. If no filters are provided, will return 0::

            # counts the number of users created in the last 24 hours
            User.query.filter(created_at=(time.time()-86400, time.time())).count()
        '''
        filters = self._filters
        if self._order_by:
            filters += (self._order_by.lstrip('-'),)
        if not filters:
            raise QueryError("You are missing filter or order criteria")
        return self._model._gindex.count(_connect(self._model), filters)

    def _search(self):
        if not (self._filters or self._order_by):
            raise QueryError("You are missing filter or order criteria")
        limit = () if not self._limit else self._limit
        return self._model._gindex.search(
            _connect(self._model), self._filters, self._order_by, *limit)

    def iter_result(self, timeout=30, pagesize=100):
        '''
        Iterate over the results of your query instead of getting them all with
        `.all()`. Will only perform a single query. If you expect that your
        processing will take more than 30 seconds to process 100 items, you
        should pass `timeout` and `pagesize` to reflect an appropriate timeout
        and page size to fetch at once.

        .. note: Limit clauses are ignored and not passed.

        Usage::

            for user in User.query.endswith(email='@gmail.com').iter_result():
                # do something with user
                ...
        '''
        key = self.cached_result(timeout)
        conn = _connect(self._model)
        for i in range(0, conn.zcard(key), pagesize):
            conn.expire(key, timeout)
            ids = conn.zrange(key, i, i+pagesize-1)
            # No need to fill up memory with paginated items hanging around the
            # session. Remove all entities from the session that are not
            # already modified (were already in the session and modified).
            for ent in self._model.get(ids):
                if not ent._modified:
                    session.forget(ent)
                yield ent

    def cached_result(self, timeout):
        '''
        This will execute the query, returning the key where a ZSET of your
        results will be stored for pagination, further operations, etc.

        The timeout must be a positive integer number of seconds for which to
        set the expiration time on the key (this is to ensure that any cached
        query results are eventually deleted, unless you make the explicit
        step to use the PERSIST command).

        .. note: Limit clauses are ignored and not passed.

        Usage::

            ukey = User.query.endswith(email='@gmail.com').cached_result(30)
            for i in xrange(0, conn.zcard(ukey), 100):
                # refresh the expiration
                conn.expire(ukey, 30)
                users = User.get(conn.zrange(ukey, i, i+99))
                ...
        '''
        if not (self._filters or self._order_by):
            raise QueryError("You are missing filter or order criteria")
        timeout = int(timeout)
        if timeout < 1:
            raise QueryError("You must specify a timeout >= 1, you gave %r"%timeout)
        return self._model._gindex.search(
            _connect(self._model), self._filters, self._order_by, timeout=timeout)

    def execute(self):
        '''
        Actually executes the query, returning any entities that match the
        filters, ordered by the specified ordering (if any), limited by any
        earlier limit calls.
        '''
        return self._model.get(self._search())

    def all(self):
        '''
        Alias for ``execute()``.
        '''
        return self.execute()

    def first(self):
        '''
        Returns only the first result from the query, if any.
        '''
        lim = [0, 1]
        if self._limit:
            lim[0] = self._limit[0]
        ids = self.limit(*lim)._search()
        if ids:
            return self._model.get(ids[0])
        return None
