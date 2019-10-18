##############################################################################
#
# Copyright (c) 2009 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
"""Pack/Undo implementations.
"""
from __future__ import absolute_import
from __future__ import print_function

import logging
import time

from ZODB.POSException import UndoError
from ZODB.utils import u64
from zope.interface import implementer

from .._compat import metricmethod
from .._compat import OidList
from .._util import byte_display
from .._util import get_memory_usage

from ..treemark import TreeMarker

from .schema import Schema
from .connections import LoadConnection
from .connections import StoreConnection
from .interfaces import IPackUndo
from ._util import DatabaseHelpersMixin
from .sql import it

# pylint:disable=too-many-lines,unused-argument


logger = logging.getLogger(__name__)

class PackUndo(DatabaseHelpersMixin):
    """Abstract base class for pack/undo"""

    _choose_pack_transaction_query = None


    _lock_for_share = 'FOR SHARE'
    _lock_for_update = 'FOR UPDATE'

    driver = None
    connmanager = None
    runner = None
    locker = None
    options = None

    cursor_arraysize = 10000

    def __init__(self, database_driver, connmanager, runner, locker, options):
        self.driver = database_driver
        self.connmanager = connmanager
        self.runner = runner
        self.locker = locker
        self.options = options

    def with_options(self, options):
        """
        Return a new instance that will use the given options, instead
        of the options originally constructed.
        """
        if options == self.options:
            # If the options haven't changed, return ourself. This is
            # for tests that make changes to the structure of this
            # object not captured in the constructor or options.
            # (checkPackWhileReferringObjectChanges)
            return self
        return self.__class__(self.driver, self.connmanager, self.runner, self.locker, options)

    def choose_pack_transaction(self, pack_point):
        """Return the transaction before or at the specified pack time.

        Returns None if there is nothing to pack.
        """
        conn, cursor = self.connmanager.open()
        try:
            self._choose_pack_transaction_query.execute(cursor, {'tid': pack_point})
            rows = cursor.fetchall()
            if not rows:
                # Nothing needs to be packed.
                return None
            return rows[0][0]
        finally:
            self.connmanager.close(conn, cursor)

    # Subclasses (notably Oracle) can define this to provide hints
    # that affect graph traversal.
    #
    # We cannot include the hints Oracle wants as standard; /*+ ... */
    # is also the syntax for a MySQL 5.7 optimizer hint, but FULL(...)
    # isn't valid syntax, so it produces a warning (and some
    # frameworks/drivers want to treat warnings as errors, or print
    # them).
    #
    # The alternate comment syntax for Oracle hints, --+ ..., isn't a
    # valid MySQL comment (MySQL requires whitespace after --) and raises
    # a syntax error.
    #
    # PostgreSQL doesn't have hints, so this is a no-op there.
    _traverse_graph_optimizer_hint = ''

    def _traverse_graph(self, load_connection, store_connection):
        """
        Visit the entire object graph to find out what should be
        kept.

        Sets the pack_object.keep flags.

        Must not read from the ``object_state`` table or any other table that
        could be inconsistent with the original snapshot view of references
        established by :meth:`pre_pack`.

        *cursor* is a writable store connection cursor.
        """
        logger.info("pre_pack: downloading pack_object and object_ref.")
        # Ensure we're up-to-date and can view the data in pack_object.
        # Note that we don't just use restart() here: if we haven't actually
        # opened the cursor yet, restart() won't do anything. But on MySQL,
        # (because we TRUNCATE'd the pack_object table?) if we don't actually
        # rollback, we get
        #   OperationalError: 1412, 'Table definition has changed, please retry transaction'
        load_connection.rollback_quietly()

        marker = TreeMarker()

        # Download the graph of object references into the TreeMarker.
        # TODO: We can probably do much or most of this in SQL, at least
        # in recent databases that support recursive WITH queries?

        # XXX: In history-free mode, ``pack_object`` contains exactly
        # the set of OIDs that are present in ``object_state`` and I
        # *think* that ``pack_object.keep_tid`` is always going to be equal to
        # ``object_ref.tid``. We may get better behaviour if we join
        # against that table here
        with load_connection.server_side_cursor() as ss_load_cursor:
            stmt = """
            SELECT {}
                zoid, object_ref.to_zoid
            FROM object_ref
                INNER JOIN pack_object USING (zoid)
            WHERE object_ref.tid >= pack_object.keep_tid
            ORDER BY object_ref.zoid, object_ref.to_zoid
            """.format(self._traverse_graph_optimizer_hint)
            ss_load_cursor.execute(stmt)

            while True:
                rows = ss_load_cursor.fetchmany(self.cursor_arraysize)
                if not rows:
                    break
                marker.add_refs(rows)

        # Use the TreeMarker to find all reachable objects, starting
        # with the ones that are known reachable. These are the roots:
        #
        # - ZOID 0 which is explicitly marked as such
        #
        # - In history preserving databases where we are not doing GC,
        #   this includes all objects (except those explicitly
        #   deleted) --- but we don't actually call this method for
        #   the HP-no-gc case.
        #
        # - In history preserving *with* gc, this is all objects that
        #   have been modified after the pack time or are referenced
        #   from objects that have been modified after the pack time.
        #
        # - In history free *with* gc, this is all objects that have
        #   been modified after the pack time.
        # XXX: It seems like a lot of what TreeMarker does could actually
        # be done in the database, especially if we have support for
        # recursive common table expressions; if we don't, we can still do more of it
        # in the DB, it will just take more queries and some temp tables.
        logger.info("pre_pack: traversing the object graph "
                    "to find reachable objects.")
        with load_connection.server_side_cursor() as ss_load_cursor:
            stmt = """
            SELECT zoid
            FROM pack_object
            WHERE keep = %(TRUE)s
            """
            self.runner.run_script_stmt(ss_load_cursor, stmt)
            while True:
                rows = ss_load_cursor.fetchmany(self.cursor_arraysize)
                if not rows:
                    break
                marker.mark(oid for (oid,) in rows)

        marker.free_refs()

        # Upload the TreeMarker results to the database.
        # TODO: It probably makes more sense to mark *unreachable* objects?
        # There should generally be fewer of them than reachable objects
        # if the database is regularly GC'd.
        logger.info(
            "pre_pack: marking objects reachable: %d",
            marker.reachable_count)

        store_cursor = store_connection.cursor
        batch = []
        def upload_batch():
            # This would be easily done in parallel.
            # Marking 30 MM objects, even in batches, takes at least
            # 30 minutes.
            # XXX: No, this is very wrong. Use the RowBatcher
            # Alternately, flush to a table and then join
            # against that at the end.
            # TODO: If we're batching, we need to log progress
            oids_str = ','.join(str(oid) for oid in batch)
            del batch[:]
            stmt = """
            UPDATE pack_object
            SET keep = %%(TRUE)s,
                visited = %%(TRUE)s
            WHERE zoid IN (%s)
            """ % oids_str
            self.runner.run_script_stmt(store_cursor, stmt)

        batch_append = batch.append
        for oid in marker.reachable:
            batch_append(oid)
            if len(batch) >= 1000:
                upload_batch()
        if batch:
            upload_batch()

    # The only things to worry about are object_state and blob_chuck
    # and, in history-preserving, transaction. blob chunks are deleted
    # automatically by a foreign key; transaction we'll handle with a
    # pack. (We don't do anything with current_object; a state of NULL
    # represents a deleted object; it shouldn't be reachable anyway
    # and will be packed away next time we pack (without GC))

    # We shouldn't *have* to verify the oldserial in the delete statement,
    # because our only consumer is zc.zodbdgc which only calls us for
    # unreachable objects, so they shouldn't be modified and get a new
    # TID. But it's safer to do so.
    _script_delete_object = None

    def deleteObject(self, cursor, oid, oldserial):
        params = {'oid': u64(oid), 'tid': u64(oldserial)}
        self.runner.run_script_stmt(
            cursor,
            self._script_delete_object,
            params)
        return cursor.rowcount

    def on_filling_object_refs_added(self, oids=None, tids=None):
        """Test injection point for packing."""

@implementer(IPackUndo)
class HistoryPreservingPackUndo(PackUndo):
    """
    History-preserving pack/undo.
    """

    keep_history = True

    _choose_pack_transaction_query = Schema.transaction.select(
        it.c.tid
    ).where(
        it.c.tid > 0
    ).and_(
        it.c.tid <= it.bindparam('tid')
    ).and_(
        it.c.packed == False  # pylint:disable=singleton-comparison
    ).order_by(
        it.c.tid, 'DESC'
    ).limit(1)

    _script_create_temp_pack_visit = """
    CREATE TEMPORARY TABLE temp_pack_visit (
        zoid BIGINT NOT NULL PRIMARY KEY,
        keep_tid BIGINT NOT NULL
    );
    CREATE INDEX temp_pack_keep_tid ON temp_pack_visit (keep_tid)
    """

    _script_create_temp_undo = """
    CREATE TEMPORARY TABLE temp_undo (
        zoid BIGINT NOT NULL,
        prev_tid BIGINT NOT NULL
    );
    CREATE UNIQUE INDEX temp_undo_zoid ON temp_undo (zoid)
    """

    _script_reset_temp_undo = "DROP TABLE temp_undo"

    _script_find_pack_tid = """
    SELECT keep_tid
    FROM pack_object
    ORDER BY keep_tid DESC
    LIMIT 1
    """

    _script_transaction_has_data = """
    SELECT 1
    FROM object_state
    WHERE tid = %(tid)s
    LIMIT 1
    """

    _script_pack_current_object = """
    DELETE FROM current_object
    WHERE tid = %(tid)s
    AND zoid in (
        SELECT pack_state.zoid
        FROM pack_state
        WHERE pack_state.tid = %(tid)s
        ORDER BY pack_state.zoid
    )
    """

    _script_pack_object_state = """
    DELETE FROM object_state
    WHERE tid = %(tid)s
    AND zoid in (
        SELECT pack_state.zoid
        FROM pack_state
        WHERE pack_state.tid = %(tid)s
        ORDER BY pack_state.zoid
    )
    """

    _script_pack_object_ref = """
    DELETE FROM object_refs_added
    WHERE tid IN (
        SELECT tid
        FROM "transaction"
        WHERE is_empty = %(TRUE)s
    );

    DELETE FROM object_ref
    WHERE tid IN (
        SELECT tid
        FROM "transaction"
        WHERE is_empty = %(TRUE)s
    );
        """

    # Previously we used `= ANY(ARRAY(...))`, as was once recommended,
    # (See http://www.postgres.cz/index.php/PostgreSQL_SQL_Tricks#Fast_first_n_rows_removing)
    # but that is no longer recommended or expected to be faster.
    # Also, it was postgres specific. Now we use a more standard syntax,
    # that lets us preserve order (in case that matters).
    _script_delete_empty_transactions_batch = """
    DELETE FROM "transaction"
    WHERE tid IN (
        SELECT tid FROM "transaction"
        WHERE packed = %(TRUE)s
        AND is_empty = %(TRUE)s
        ORDER BY tid
        LIMIT 1000
    )
    """

    _script_delete_object = """
    UPDATE object_state
    SET state = NULL,
        state_size = 0,
        md5 = ''
    WHERE zoid = %(oid)s
    AND    tid = %(tid)s
    """

    _is_packed_tx_query = Schema.transaction.select(
        1
    ).where(
        Schema.transaction.c.tid == Schema.transaction.bindparam('undo_tid')
    ).and_(
        Schema.transaction.c.packed == False # pylint:disable=singleton-comparison
    )

    _is_root_creation_tx_query = Schema.object_state.select(
        1
    ).where(
        Schema.object_state.c.tid == Schema.object_state.bindparam('undo_tid')
    ).and_(
        Schema.object_state.c.zoid == 0
    ).and_(
        Schema.object_state.c.prev_tid == 0
    )

    @metricmethod
    def verify_undoable(self, cursor, undo_tid):
        """Raise UndoError if it is not safe to undo the specified txn."""
        self._is_packed_tx_query.execute(cursor, {'undo_tid': undo_tid})
        if not cursor.fetchall():
            raise UndoError("Transaction not found or packed")

        # Rule: we can undo an object if the object's state in the
        # transaction to undo matches the object's current state. If
        # any object in the transaction does not fit that rule, refuse
        # to undo. In theory this means arbitrary transactions can be
        # undone (because we actually match the MD5 of the state); in practice it
        # means that it must be the most recent transaction those
        # objects were involved in.

        # (Note that this prevents conflict-resolving undo as described
        # by ZODB.tests.ConflictResolution.ConflictResolvingTransUndoStorage.
        # Do people need that? If so, we can probably support it, but it
        # will require additional code.)
        stmt = """
        SELECT prev_os.zoid, current_object.tid
        FROM object_state prev_os
        INNER JOIN object_state cur_os
            ON (prev_os.zoid = cur_os.zoid)
        INNER JOIN current_object
            ON (cur_os.zoid = current_object.zoid
                AND cur_os.tid = current_object.tid)
        WHERE prev_os.tid = %s
            AND cur_os.md5 != prev_os.md5
        ORDER BY prev_os.zoid
        """
        cursor.execute(stmt, (undo_tid,))
        if cursor.fetchmany():
            raise UndoError(
                "Some data were modified by a later transaction")

        # Rule: don't allow the creation of the root object to
        # be undone.  It's hard to get it back.
        self._is_root_creation_tx_query.execute(cursor, {'undo_tid': undo_tid})
        if cursor.fetchall():
            raise UndoError("Can't undo the creation of the root object")


    @metricmethod
    def undo(self, cursor, undo_tid, self_tid):
        """Undo a transaction.

        Parameters: "undo_tid", the integer tid of the transaction to undo,
        and "self_tid", the integer tid of the current transaction.

        Returns the states copied forward by the undo operation as a
        list of (oid, old_tid).
        """
        stmt = self._script_create_temp_undo
        if stmt:
            self.runner.run_script(cursor, stmt)

        stmt = """
        DELETE FROM temp_undo;

        -- Put into temp_undo the list of objects to be undone and
        -- the tid of the transaction that has the undone state.
        INSERT INTO temp_undo (zoid, prev_tid)
        SELECT zoid, prev_tid
        FROM object_state
        WHERE tid = %(undo_tid)s
        ORDER BY zoid;

        -- Override previous undo operations within this transaction
        -- by resetting the current_object pointer and deleting
        -- copied states from object_state.
        UPDATE current_object
        SET tid = (
            SELECT prev_tid
            FROM object_state
            WHERE zoid = current_object.zoid
              AND tid = %(self_tid)s
        )
        WHERE zoid IN (SELECT zoid FROM temp_undo ORDER BY zoid)
        AND tid = %(self_tid)s;

        DELETE FROM object_state
        WHERE zoid IN (SELECT zoid FROM temp_undo ORDER BY zoid)
            AND tid = %(self_tid)s;

        -- Copy old states forward.
        INSERT INTO object_state (zoid, tid, prev_tid, md5, state_size, state)
        SELECT temp_undo.zoid, %(self_tid)s, current_object.tid,
            md5, COALESCE(state_size, 0), state
        FROM temp_undo
        INNER JOIN current_object ON (temp_undo.zoid = current_object.zoid)
        LEFT OUTER JOIN object_state
            ON (object_state.zoid = temp_undo.zoid
                AND object_state.tid = temp_undo.prev_tid)
        ORDER BY current_object.zoid;

        -- Copy old blob chunks forward.
        INSERT INTO blob_chunk (zoid, tid, chunk_num, chunk)
        SELECT temp_undo.zoid, %(self_tid)s, chunk_num, chunk
        FROM temp_undo
            JOIN blob_chunk
                ON (blob_chunk.zoid = temp_undo.zoid
                    AND blob_chunk.tid = temp_undo.prev_tid);

        -- List the copied states.
        SELECT zoid, prev_tid
        FROM temp_undo;
        """
        self.runner.run_script(cursor, stmt,
                               {'undo_tid': undo_tid, 'self_tid': self_tid})
        res = list(cursor)

        stmt = self._script_reset_temp_undo
        if stmt:
            self.runner.run_script(cursor, stmt)

        return res

    def fill_object_refs(self, load_connection, store_connection, get_references):
        """Update the object_refs table by analyzing new transactions."""
        with load_connection.server_side_cursor() as ss_load_cursor:
            ss_load_cursor.itersize = ss_load_cursor.arraysize = self.cursor_arraysize
            stmt = """
            SELECT tx.tid
            FROM \"transaction\" tx
            LEFT OUTER JOIN object_refs_added
                ON (tx.tid = object_refs_added.tid)
            WHERE object_refs_added.tid IS NULL
            ORDER BY tx.tid
            """
            ss_load_cursor.execute(stmt)
            tids = OidList((tid for (tid,) in ss_load_cursor))
        log_at = time.time() + 60
        tid_count = len(tids)
        txns_done = 0
        self.on_filling_object_refs_added(tids=tids)
        logger.info(
            "pre_pack: analyzing references from objects in %d new "
            "transaction(s)", tid_count)
        for tid in tids:
            self._add_refs_for_tid(load_connection, store_connection, tid, get_references)
            txns_done += 1
            now = time.time()
            if now >= log_at:
                # save the work done so far
                store_connection.commit()
                log_at = now + 60
                logger.info(
                    "pre_pack: transactions analyzed: %d/%d",
                    txns_done, tid_count)
        store_connection.commit()
        logger.info("pre_pack: transactions analyzed: %d/%d", txns_done, tid_count)

    _get_objects_in_transaction_query = Schema.object_state.select(
        it.c.zoid,
        it.c.state
    ).where(
        it.c.tid == it.bindparam('tid')
    ).order_by(it.c.zoid).prepared()

    _delete_refs_for_transaction_query = Schema.object_ref.delete(
    ).where(
        it.c.tid == it.bindparam('tid')
    ).prepared()

    _insert_object_refs_added_for_transaction_query = Schema.object_refs_added.insert(
        it.c.tid
    ).prepared()

    def _add_refs_for_tid(self, load_connection, store_connection, tid, get_references):
        """Fill object_refs with all states for a transaction.

        Returns the number of references added.
        """
        logger.debug("pre_pack: transaction %d: computing references ", tid)
        from_count = 0
        self._get_objects_in_transaction_query.execute(load_connection.cursor,
                                                       {'tid': tid})

        add_rows = []  # [(from_oid, tid, to_oid)]
        for from_oid, state in load_connection.cursor:
            state = self.driver.binary_column_as_state_type(state)
            if state:
                assert isinstance(state, self.driver.state_types), type(state)
                from_count += 1
                try:
                    to_oids = get_references(state)
                except:
                    logger.exception(
                        "pre_pack: can't unpickle "
                        "object %d in transaction %d; state length = %d",
                        from_oid, tid, len(state))
                    raise
                for to_oid in to_oids:
                    add_rows.append((from_oid, tid, to_oid))

        # A previous pre-pack may have been interrupted.  Delete rows
        # from the interrupted attempt.
        self._delete_refs_for_transaction_query.execute(store_connection.cursor,
                                                        {'tid': tid})

        # Add the new references.
        # TODO: Use RowBatcher?
        stmt = """
        INSERT INTO object_ref (zoid, tid, to_zoid)
        VALUES (%s, %s, %s)
        """
        self.runner.run_many(store_connection.cursor, stmt, add_rows)

        # The references have been computed for this transaction
        self._insert_object_refs_added_for_transaction_query.execute(store_connection.cursor,
                                                                     (tid,))

        to_count = len(add_rows)
        logger.debug("pre_pack: transaction %d: has %d reference(s) "
                     "from %d object(s)", tid, to_count, from_count)
        return to_count

    @metricmethod
    def pre_pack(self, pack_tid, get_references):
        """Decide what to pack.

        pack_tid specifies the most recent transaction to pack.

        get_references is a function that accepts a pickled state and
        returns a set of OIDs that state refers to.

        The self.options.pack_gc flag indicates whether
        to run garbage collection.
        If pack_gc is false, at least one revision of every object is kept,
        even if nothing refers to it.  Packing with pack_gc disabled can be
        much faster.
        """
        load_connection = LoadConnection(self.connmanager)
        store_connection = StoreConnection(self.connmanager)
        try:
            # The pre-pack functions are responsible for managing
            # their own commits; when they return, the transaction
            # should be committed.
            #
            # ``pack_object`` should be populated,
            # essentially with the distinct list of all objects and their
            # maximum (newest) transaction ids.
            if self.options.pack_gc:
                logger.info("pre_pack: start with gc enabled")
                self._pre_pack_with_gc(
                    load_connection, store_connection, pack_tid, get_references)
            else:
                logger.info("pre_pack: start without gc")
                self._pre_pack_without_gc(
                    load_connection, store_connection, pack_tid)

            logger.info("pre_pack: enumerating states to pack")
            cursor = store_connection.cursor
            stmt = "%(TRUNCATE)s pack_state"
            self.runner.run_script_stmt(cursor, stmt)
            to_remove = 0


            if self.options.pack_gc:
                # Mark all objects we said not to keep as something
                # we should discard.
                stmt = """
                INSERT INTO pack_state (tid, zoid)
                SELECT tid, zoid
                FROM object_state
                INNER JOIN pack_object USING (zoid)
                WHERE keep = %(FALSE)s
                    AND tid > 0
                    AND tid <= %(pack_tid)s
                ORDER BY zoid
                """
                self.runner.run_script_stmt(
                    cursor, stmt, {'pack_tid': pack_tid})
                to_remove += cursor.rowcount
            else:
                # Support for IExternalGC. Also remove deleted objects.
                stmt = """
                INSERT INTO pack_state (tid, zoid)
                SELECT t.tid, t.zoid
                FROM (
                    SELECT zoid, tid
                    FROM object_state
                    WHERE state IS NULL
                    AND tid = (
                        SELECT MAX(i.tid)
                        FROM object_state i
                        WHERE i.zoid = object_state.zoid
                    )
                ) t
                """
                self.runner.run_script_stmt(cursor, stmt)
                to_remove += cursor.rowcount

            # Pack object states with the keep flag set to true,
            # excluding their current TID.
            stmt = """
            INSERT INTO pack_state (tid, zoid)
            SELECT tid, zoid
            FROM object_state
            INNER JOIN pack_object USING (zoid)
            WHERE keep = %(TRUE)s
                AND tid > 0
                AND tid != keep_tid
                AND tid <= %(pack_tid)s
            ORDER BY zoid
            """
            self.runner.run_script_stmt(
                cursor, stmt, {'pack_tid': pack_tid})
            to_remove += cursor.rowcount

            # Make a simple summary of the transactions to examine.
            logger.info("pre_pack: enumerating transactions to pack")
            stmt = "%(TRUNCATE)s pack_state_tid"
            self.runner.run_script_stmt(cursor, stmt)
            stmt = """
            INSERT INTO pack_state_tid (tid)
            SELECT DISTINCT tid
            FROM pack_state
            """
            cursor.execute(stmt)

            logger.info("pre_pack: will remove %d object state(s)",
                        to_remove)

            logger.info("pre_pack: finished successfully")
            store_connection.commit()
        except:
            store_connection.rollback_quietly()
            raise
        finally:
            store_connection.drop()
            load_connection.drop()

    def __initial_populate_pack_object(self, load_connection, store_connection,
                                       pack_tid, keep):
        """
        Put all objects into ``pack_object`` that have revisions equal
        to or below *pack_tid*, setting their initial ``keep`` status
        to *keep*.

        Commits the transaction to release locks.
        """
        # Access the tables that are used by online transactions
        # in a short transaction and immediately commit to release any
        # locks.

        # TRUNCATE may or may not cause implicit commits. (MySQL: Yes,
        # PostgreSQL: No)
        self.runner.run_script(store_connection.cursor, "%(TRUNCATE)s pack_object;")

        affected_objects = """
        SELECT zoid, tid
        FROM object_state
        WHERE tid > 0 AND tid <= %(pack_tid)s
        ORDER BY zoid
        """

        # Take the locks we need up front, in order, because
        # locking in a subquery doing an INSERT isn't guaranteed to use that
        # order (deadlocks seen with commits on MySQL 5.7 without this,
        # when using REPEATABLE READ.)
        #
        # We must do this on its own, because some drivers (notably
        # mysql-connector-python) get very upset
        # ("mysql.connector.errors.InternalError: Unread result
        # found") if you issue a SELECT that you don't then consume.
        #
        # Since we switched MySQL back to READ COMMITTED (what PostgreSQL uses)
        # I haven't been able to produce the error anymore. So don't explicitly lock.

        stmt = """
        INSERT INTO pack_object (zoid, keep, keep_tid)
        SELECT zoid, """ + ('%(TRUE)s' if keep else '%(FALSE)s') + """, MAX(tid)
        FROM ( """ + affected_objects + """ ) t
        GROUP BY zoid;

        -- Keep the root object.
        UPDATE pack_object
        SET keep = %(TRUE)s
        WHERE zoid = 0;
        """
        self.runner.run_script(store_connection.cursor, stmt, {'pack_tid': pack_tid})
        store_connection.commit()

    def _pre_pack_without_gc(self, conn, cursor, pack_tid):
        """
        Determine what to pack, without garbage collection.

        With garbage collection disabled, there is no need to follow
        object references.
        """
        # Fill the pack_object table with OIDs, but configure them
        # all to be kept by setting keep to true.
        logger.debug("pre_pack: populating pack_object")
        self.__initial_populate_pack_object(conn, cursor, pack_tid, keep=True)

    def _pre_pack_with_gc(self, load_connection, store_connection,
                          pack_tid, get_references):
        """
        Determine what to pack, with garbage collection.
        """
        stmt = self._script_create_temp_pack_visit
        if stmt:
            self.runner.run_script(store_connection.cursor, stmt)

        self.fill_object_refs(load_connection, store_connection, get_references)

        logger.info("pre_pack: filling the pack_object table")
        # Fill the pack_object table with OIDs that either will be
        # removed (if nothing references the OID) or whose history will
        # be cut.
        self.__initial_populate_pack_object(load_connection, store_connection,
                                            pack_tid, keep=False)

        stmt = """
        -- Keep objects that have been revised since pack_tid.
        -- Use temp_pack_visit for temporary state; otherwise MySQL 5 chokes.
        INSERT INTO temp_pack_visit (zoid, keep_tid)
        SELECT zoid, 0
        FROM current_object
        WHERE tid > %(pack_tid)s
        ORDER BY zoid;

        UPDATE pack_object
        SET keep = %(TRUE)s
        WHERE zoid IN (
            SELECT zoid
            FROM temp_pack_visit
        );

        %(TRUNCATE)s temp_pack_visit;

        -- Keep objects that are still referenced by object states in
        -- transactions that will not be packed.
        -- Use temp_pack_visit for temporary state; otherwise MySQL 5 chokes.
        INSERT INTO temp_pack_visit (zoid, keep_tid)
        SELECT DISTINCT to_zoid, 0
        FROM object_ref
        WHERE tid > %(pack_tid)s;

        UPDATE pack_object
        SET keep = %(TRUE)s
        WHERE zoid IN (
            SELECT zoid
            FROM temp_pack_visit
        );

        %(TRUNCATE)s temp_pack_visit;
        """
        self.runner.run_script(store_connection.cursor, stmt, {'pack_tid': pack_tid})

        # Traverse the graph, setting the 'keep' flags in pack_object
        self._traverse_graph(load_connection, store_connection)
        store_connection.commit()

    def _find_pack_tid(self):
        """If pack was not completed, find our pack tid again"""
        conn, cursor = self.connmanager.open_for_pre_pack()
        try:
            stmt = self._script_find_pack_tid
            self.runner.run_script_stmt(cursor, stmt)
            res = [tid for (tid,) in cursor]
        finally:
            self.connmanager.close(conn, cursor)
        return res[0] if res else 0


    @metricmethod
    def pack(self, pack_tid, packed_func=None):
        """Pack.  Requires the information provided by pre_pack."""
        # pylint:disable=too-many-locals
        # Read committed mode is sufficient.

        conn, cursor = self.connmanager.open_for_store()
        try: # pylint:disable=too-many-nested-blocks
            try:
                # If we have a transaction entry in ``pack_state_tid`` (that is,
                # we found a transaction with an object in the range of transactions
                # we can pack away) that matches an actual transaction entry (XXX:
                # How could we be in the state where the transaction row is gone but we still
                # have object_state with that transaction id?), then we need to pack that
                # transaction. The presence of an entry in ``pack_state_tid`` means that all
                # object states from that transaction should be removed.
                stmt = """
                SELECT tx.tid,
                       CASE WHEN packed = %(TRUE)s THEN 1 ELSE 0 END,
                       CASE WHEN pack_state_tid.tid IS NOT NULL THEN 1 ELSE 0 END
                FROM "transaction" tx
                LEFT OUTER JOIN pack_state_tid ON (tx.tid = pack_state_tid.tid)
                WHERE tx.tid > 0
                    AND tx.tid <= %(pack_tid)s
                    AND (packed = %(FALSE)s OR pack_state_tid.tid IS NOT NULL)
                ORDER BY tx.tid
                """
                self.runner.run_script_stmt(
                    cursor, stmt, {'pack_tid': pack_tid})
                tid_rows = list(cursor) # oldest first, sorted in SQL

                total = len(tid_rows)
                logger.info("pack: will pack %d transaction(s)", total)

                stmt = self._script_create_temp_pack_visit
                if stmt:
                    self.runner.run_script(cursor, stmt)

                # Lock and delete rows in the same order that
                # new commits would in order to prevent deadlocks.
                # Pack in small batches of transactions only after we are able
                # to obtain a commit lock in order to minimize the
                # interruption of concurrent write operations.
                start = time.time()
                packed_list = []
                counter, lastreport, statecounter = 0, 0, 0
                # We'll report on progress in at most .1% step increments
                reportstep = max(total / 1000, 1)

                for tid, packed, has_removable in tid_rows:
                    self._pack_transaction(
                        cursor, pack_tid, tid, packed, has_removable,
                        packed_list)
                    counter += 1
                    if time.time() >= start + self.options.pack_batch_timeout:
                        self.connmanager.commit(conn, cursor)
                        if packed_func is not None:
                            for poid, ptid in packed_list:
                                packed_func(poid, ptid)
                        statecounter += len(packed_list)
                        if counter >= lastreport + reportstep:
                            logger.info("pack: packed %d (%.1f%%) transaction(s), "
                                        "affecting %d states",
                                        counter, counter / float(total) * 100,
                                        statecounter)
                            lastreport = counter / reportstep * reportstep
                        del packed_list[:]
                        start = time.time()
                if packed_func is not None:
                    for oid, tid in packed_list:
                        packed_func(oid, tid)
                packed_list = None

                self._pack_cleanup(conn, cursor)

            except:
                logger.exception("pack: failed")
                self.connmanager.rollback_quietly(conn, cursor)
                raise

            else:
                logger.info("pack: finished successfully")
                self.connmanager.commit(conn, cursor)

        finally:
            self.connmanager.close(conn, cursor)


    def _pack_transaction(self, cursor, pack_tid, tid, packed,
                          has_removable, packed_list):
        """
        Pack one transaction. Requires populated pack tables.

        If *has_removable* is true, then we have object states and current
        object pointers to remove.
        """
        logger.debug("pack: transaction %d: packing", tid)
        removed_objects = 0
        removed_states = 0

        if has_removable:
            stmt = self._script_pack_current_object
            self.runner.run_script_stmt(cursor, stmt, {'tid': tid})
            removed_objects = cursor.rowcount

            stmt = self._script_pack_object_state
            self.runner.run_script_stmt(cursor, stmt, {'tid': tid})
            removed_states = cursor.rowcount

            # Terminate prev_tid chains
            stmt = """
            UPDATE object_state SET prev_tid = 0
            WHERE prev_tid = %(tid)s
                AND tid <= %(pack_tid)s
            """
            self.runner.run_script_stmt(cursor, stmt,
                                        {'pack_tid': pack_tid, 'tid': tid})

            stmt = """
            SELECT pack_state.zoid
            FROM pack_state
            WHERE pack_state.tid = %(tid)s
            """
            self.runner.run_script_stmt(cursor, stmt, {'tid': tid})
            for (oid,) in cursor:
                packed_list.append((oid, tid))

        # Find out whether the transaction is empty
        stmt = self._script_transaction_has_data
        self.runner.run_script_stmt(cursor, stmt, {'tid': tid})
        empty = not list(cursor)

        # mark the transaction packed and possibly empty
        if empty:
            clause = 'is_empty = %(TRUE)s'
            state = 'empty'
        else:
            clause = 'is_empty = %(FALSE)s'
            state = 'not empty'
        stmt = 'UPDATE "transaction" SET packed = %(TRUE)s, ' + clause
        stmt += " WHERE tid = %(tid)s"
        self.runner.run_script_stmt(cursor, stmt, {'tid': tid})

        logger.debug(
            "pack: transaction %d (%s): removed %d object(s) and %d state(s)",
            tid, state, removed_objects, removed_states)


    def _pack_cleanup(self, conn, cursor):
        """Remove unneeded table rows after packing"""
        # commit the work done so far, releasing row-level locks.
        self.connmanager.commit(conn, cursor)
        logger.info("pack: cleaning up")

        # This section does not need to hold the commit lock, as it only
        # touches pack-specific tables. We already hold a pack lock for that.
        logger.debug("pack: removing unused object references")
        stmt = self._script_pack_object_ref
        self.runner.run_script(cursor, stmt)

        # We need a commit lock when touching the transaction table though.
        # We'll do it in batches of 1000 rows.
        logger.debug("pack: removing empty packed transactions")
        while True:
            stmt = self._script_delete_empty_transactions_batch
            self.runner.run_script_stmt(cursor, stmt)
            deleted = cursor.rowcount
            self.connmanager.commit(conn, cursor)
            self.locker.release_commit_lock(cursor)
            if deleted < 1000:
                # Last set of deletions complete
                break

        # perform cleanup that does not require the commit lock
        logger.debug("pack: clearing temporary pack state")
        for _table in ('pack_object', 'pack_state', 'pack_state_tid'):
            stmt = '%(TRUNCATE)s ' + _table
            self.runner.run_script_stmt(cursor, stmt)


@implementer(IPackUndo)
class HistoryFreePackUndo(PackUndo):
    """
    History-free pack/undo.
    """

    keep_history = False

    # How often, in seconds, to commit work in progress.
    # This is a variable here for testing.
    fill_object_refs_commit_frequency = 60

    # How many object states to find references in at any one time.
    # This is a control on the amount of memory used by the Python
    # process during packing, especially if the database driver
    # doesn't use server-side cursors.
    fill_object_refs_batch_size = 100

    _choose_pack_transaction_query = Schema.object_state.select(
        it.c.tid
    ).where(
        it.c.tid > 0
    ).and_(
        it.c.tid <= it.bindparam('tid')
    ).order_by(
        it.c.tid, 'DESC'
    ).limit(1)

    # history-free packing doesn't use temp_pack_visit, unlike
    # history-preserving.
    # _script_create_temp_pack_visit = """
    #     CREATE TEMPORARY TABLE temp_pack_visit (
    #         zoid BIGINT NOT NULL PRIMARY KEY,
    #         keep_tid BIGINT NOT NULL
    #     );
    #     CREATE INDEX temp_pack_keep_tid ON temp_pack_visit (keep_tid)
    #     """

    _script_delete_object = """
    DELETE FROM object_state
    WHERE zoid = %(oid)s
    and tid = %(tid)s
    """

    def on_fill_object_ref_batch(self, oid_batch, refs_found):
        """Hook for testing."""

    def verify_undoable(self, cursor, undo_tid):
        """Raise UndoError if it is not safe to undo the specified txn."""
        raise UndoError("Undo is not supported by this storage")

    def undo(self, cursor, undo_tid, self_tid):
        """Undo a transaction.

        Parameters: "undo_tid", the integer tid of the transaction to undo,
        and "self_tid", the integer tid of the current transaction.

        Returns the list of OIDs undone.
        """
        raise UndoError("Undo is not supported by this storage")

    def fill_object_refs(self, load_connection, store_connection, get_references):
        """
        Update the object_refs table by analyzing new object states.

        See :meth:`pre_pack` for a description of the parameters.

        Because *load_connection* is read-only and repeatable read,
        we don't need to do any object-level locking.
        """
        # Begin by ensuring we have a snapshot reflecting anything
        # committed up to this point, including the contents of
        # ``pack_object``, which determines the visible objects
        # we will examine.
        load_connection.restart()
        mem_begin = get_memory_usage()
        logger.debug("pre_pack: Collecting objects to examine.")
        # Recall pre_pack can be run many times.
        # Ordering should be immaterial as we are in a read-only snapshot view
        # of the database; we shouldn't run into locking issues with other
        # transactions.
        with load_connection.server_side_cursor() as ss_load_cursor:
            ss_load_cursor.itersize = ss_load_cursor.arraysize = self.cursor_arraysize
            stmt = """
            SELECT zoid
            FROM pack_object
            INNER JOIN object_state USING (zoid)
            LEFT OUTER JOIN object_refs_added
                USING (zoid)
            WHERE object_refs_added.tid IS NULL
              OR object_refs_added.tid != object_state.tid
            """
            ss_load_cursor.execute(stmt)
            logger.debug(
                "pre_pack: Selected objects to examine (memory delta: %s)",
                byte_display(get_memory_usage() - mem_begin)
            )

            oids = OidList((row[0] for row in ss_load_cursor))

        log_at = time.time() + self.fill_object_refs_commit_frequency
        self.on_filling_object_refs_added(oids=oids)

        oid_count = len(oids)
        oids_done = 0
        num_refs_found = 0
        # Against 30 MM rows with MySQL on Python 2.7 with the
        # server-side cursor on mysqlclient, the SELECT takes
        # negligible time and memory; transforming into the
        # array.array and pulling rows takes 5 minutes and a total of
        # 231MB with a batch size of 1024; that's about the resident
        # size of the process, too. Using a fetch size of 10000 didn't reduce
        # the time substantially though curiously it did report just 165.9MB
        # memory delta.
        #
        # Previously, using a list and a buffered cursor for the same setup,
        # the SELECT takes 3.5 minutes and a memory delta of 2.5GB; transforming into
        # the list takes a few more seconds and a final delta of 3GB. When the cursor
        # is closed, the resident size of the process shrinks to around 1.2GB.
        logger.info(
            "pre_pack: analyzing references from %d object(s) (memory delta: %s)",
            oid_count, byte_display(get_memory_usage() - mem_begin))
        while oids_done < oid_count:
            # Previously, we iterated like this:
            #
            # while oids:
            #    batch = oids[:batch_size]
            #    oids =  oids[batch_size:]
            #
            # However, that turns into O(n^2) operations with a large
            # overhead, especially on CPython. Each slice operation
            # allocates a new list, and copies into it (including
            # INCREF). Even with the O(n^2) algorithm,
            # array.array('Q') benchmarks ~5x faster for these two
            # operations, probably because it's just memory movements,
            # not loops that have to INCREF/DECREF.
            #
            # A simple profile while this is running with 30 MM rows
            # shows at least 37% of the time spent in the C
            # ``list_slice`` function and 31% in ``list_dealloc``,
            # averaging about 15,000 objects per minute.
            #
            # Switching just to array.array and leaving the slicing, I
            # was getting 44,000 objects per minute, but 99% time
            # spent in memmove().
            #
            # Using manual indexing of arrays, CPU usage of less than
            # 35%; for the first time, 35% of profile time is spent in
            # talking to MySQL (over gigabit switch); clearly parallel
            # pre-fetching would be useful.

            batch = oids[oids_done:oids_done + self.fill_object_refs_batch_size]
            oids_done += len(batch)

            refs_found = self._add_refs_for_oids(load_connection, store_connection,
                                                 batch, get_references)
            num_refs_found += len(refs_found)
            self.on_fill_object_ref_batch(oid_batch=batch, refs_found=refs_found)

            now = time.time()
            if now >= log_at:
                # Save the work done so far.
                store_connection.commit()
                log_at = now + self.fill_object_refs_commit_frequency
                logger.info(
                    "pre_pack: objects analyzed: %d/%d (%d total references)",
                    oids_done, oid_count, num_refs_found)
        # Those 30MM objects wound up with about 48,976,835 references.
        store_connection.commit()
        logger.info(
            "pre_pack: objects analyzed: %d/%d", oids_done, oid_count)

    def _add_refs_for_oids(self, load_connection, store_connection,
                           oids, get_references):
        """Fill object_refs with the states for some objects.

        Returns the number of references added.
        """
        # TODO: Use the row batcher's SELECT FROM
        load_cursor = load_connection.cursor
        oid_list = ','.join(str(oid) for oid in oids)
        stmt = """
            SELECT zoid, tid, state
            FROM object_state
            WHERE zoid IN (%s)
            ORDER BY zoid
            """ % oid_list
        self.runner.run_script_stmt(load_cursor, stmt)

        add_objects = []
        add_refs = []

        for from_oid, tid, state in load_cursor:
            state = self.driver.binary_column_as_state_type(state)
            add_objects.append((from_oid, tid))
            if state:
                try:
                    to_oids = get_references(state)
                except:
                    logger.exception(
                        "pre_pack: can't unpickle "
                        "object %d in transaction %d; state length = %d",
                        from_oid, tid, len(state)
                    )
                    raise
                for to_oid in to_oids:
                    add_refs.append((from_oid, tid, to_oid))
        if not add_objects:
            assert not add_refs
            return add_refs

        # TODO: RowBatcher for all of these
        store_cursor = store_connection.cursor
        stmt = "DELETE FROM object_refs_added WHERE zoid IN (%s)" % oid_list
        self.runner.run_script_stmt(store_cursor, stmt)
        stmt = "DELETE FROM object_ref WHERE zoid IN (%s)" % oid_list
        self.runner.run_script_stmt(store_cursor, stmt)

        stmt = """
        INSERT INTO object_ref (zoid, tid, to_zoid) VALUES (%s, %s, %s)
        """
        self.runner.run_many(store_cursor, stmt, add_refs)

        stmt = """
        INSERT INTO object_refs_added (zoid, tid) VALUES (%s, %s)
        """
        self.runner.run_many(store_cursor, stmt, add_objects)

        return add_refs

    @metricmethod
    def pre_pack(self, pack_tid, get_references):
        """
        Decide what the garbage collector should delete.

        Objects created or modified after pack_tid will not be garbage
        collected.

        get_references is a function that accepts a pickled state and
        returns a set of OIDs that state refers to.

        The self.options.pack_gc flag indicates whether to run garbage
        collection. If pack_gc is false, this method does nothing.
        """
        if not self.options.pack_gc:
            logger.warning("pre_pack: garbage collection is disabled on a "
                           "history-free storage, so doing nothing")
            return

        load_connection = LoadConnection(self.connmanager)
        store_connection = StoreConnection(self.connmanager)
        try:
            try:
                self._pre_pack_main(load_connection, store_connection,
                                    pack_tid, get_references)
            except:
                logger.exception("pre_pack: failed")
                store_connection.rollback_quietly()
                raise
            else:
                store_connection.commit()
                logger.info("pre_pack: finished successfully")
        finally:
            load_connection.drop()
            store_connection.drop()


    def _pre_pack_main(self, load_connection, store_connection,
                       pack_tid, get_references):
        """
        Determine what to garbage collect.

        *load_connection* is a
        :class:`relstorage.adapters.connections.LoadConnection`; this
        connection is in "snapshot" mode and is used to read a
        consistent view of the database. Although this connection is
        never committed or rolled back while this method is running
        (which may take a long time), because load connections are
        declared to be read-only the database engines can make certain
        optimizations that reduce the overhead of them (e.g.,
        https://dev.mysql.com/doc/refman/5.7/en/innodb-performance-ro-txn.html),
        making long-running transactions less problematic. For
        example, while packing a 60 million row single MySQL storage
        with ``zc.zodbdgc``, a load transaction was open and actively
        reading for over 8 hours while the database continued to be
        heavily written to without causing any problems.

        *store_connection* is a standard read-committed store connection;
        it will be periodically committed.
        """
        # First, fill the ``pack_object`` table with all known OIDs
        # as they currently exist in the database, regardless of
        # what the load_connection snapshot can see (which is no later
        # and possibly earlier, than what the store connection can see).
        #
        # Mark things that need to be kept:
        # - the root object;
        # - anything that has changed since ``pack_tid``;
        # Note that we do NOT add items that have been newly added since
        # ``pack_tid``; no need to traverse into them, they couldn't possibly
        # have a reference to an older object that's not also referenced
        # by an object in the snapshot (without the app doing something seriously
        # wrong): plus, we didn't find references from that item anyway.
        #
        # TODO: Copying 30MM objects takes almost 10 minutes (600s)
        # against mysql 8 running on an SSD, and heaven forgive you if
        # you kill the transaction and roll back --- the undo info is
        # insane. What if we CREATE AS SELECT a table? Doing 'CREATE
        # TEMPORARY TABLE AS' takes 173s; doing 'CREATE TABLE AS'
        # takes 277s.
        #
        # On PostgreSQL we could use unlogged tables.
        logger.info("pre_pack: filling the pack_object table")
        stmt = """
        %(TRUNCATE)s pack_object;

        INSERT INTO pack_object (zoid, keep, keep_tid)
        SELECT zoid, CASE WHEN tid > %(pack_tid)s THEN %(TRUE)s ELSE %(FALSE)s END, tid
        FROM object_state;

        -- Also keep the root
        UPDATE pack_object
        SET keep = %(TRUE)s
        WHERE zoid = 0;
        """
        self.runner.run_script(store_connection.cursor, stmt, {'pack_tid': pack_tid})
        store_connection.commit()
        logger.info("pre_pack: Filled the pack_object table")

        # Chase down all the references using a consistent snapshot, including
        # only the objects that were visible in ``pack_object``.
        self.fill_object_refs(load_connection, store_connection, get_references)

        # Traverse the graph, setting the 'keep' flags in ``pack_object``
        self._traverse_graph(load_connection, store_connection)


    def _find_pack_tid(self):
        """If pack was not completed, find our pack tid again"""

        # pack (below) ignores its pack_tid argument, so we can safely
        # return None here
        return None


    @metricmethod
    def pack(self, pack_tid, packed_func=None):
        """Run garbage collection.

        Requires the information provided by pre_pack.
        """
        # pylint:disable=too-many-locals
        # Read committed mode is sufficient.
        conn, cursor = self.connmanager.open_for_store()
        try: # pylint:disable=too-many-nested-blocks
            try:
                stmt = """
                SELECT zoid, keep_tid
                FROM pack_object
                WHERE keep = %(FALSE)s
                ORDER BY zoid
                """
                self.runner.run_script_stmt(cursor, stmt)
                to_remove = list(cursor)

                total = len(to_remove)
                logger.info("pack: will remove %d object(s)", total)

                # Hold the commit lock while packing to prevent deadlocks.
                # Pack in small batches of transactions only after we are able
                # to obtain a commit lock in order to minimize the
                # interruption of concurrent write operations.
                start = time.time()
                packed_list = []
                # We'll report on progress in at most .1% step increments
                lastreport, reportstep = 0, max(total / 1000, 1)

                while to_remove:
                    # TODO: Use the row batcher for this,
                    # or simply do a join against the table.
                    items = to_remove[:100]
                    del to_remove[:100]
                    # XXX: History free. We shouldn't need to include the TID,
                    # but that's probably there to protect against concurrent modification
                    # of the object. With our row-level locking in 3.0 this may not strictly be
                    # necessary?
                    stmt = """
                    DELETE FROM object_state
                    WHERE zoid = %s AND tid = %s
                    """
                    self.runner.run_many(cursor, stmt, items)
                    packed_list.extend(items)

                    if time.time() >= start + self.options.pack_batch_timeout:
                        self.connmanager.commit(conn, cursor)
                        if packed_func is not None:
                            for oid, tid in packed_list:
                                packed_func(oid, tid)
                        del packed_list[:]
                        counter = total - len(to_remove)
                        if counter >= lastreport + reportstep:
                            logger.info("pack: removed %d (%.1f%%) state(s)",
                                        counter, counter / float(total) * 100)
                            lastreport = counter / reportstep * reportstep
                        start = time.time()

                if packed_func is not None:
                    for oid, tid in packed_list:
                        packed_func(oid, tid)
                packed_list = None

                self._pack_cleanup(conn, cursor)

            except:
                logger.exception("pack: failed")
                self.connmanager.rollback_quietly(conn, cursor)
                raise

            else:
                logger.info("pack: finished successfully")
                self.connmanager.commit(conn, cursor)
        finally:
            self.connmanager.close(conn, cursor)


    def _pack_cleanup(self, conn, cursor):
        # commit the work done so far
        self.connmanager.commit(conn, cursor)
        self.locker.release_commit_lock(cursor)
        logger.info("pack: cleaning up")

        # This section does not need to hold the commit lock, as it only
        # touches pack-specific tables. We already hold a pack lock for that.
        # XXX: Shouldn't we keep this? Unless there's a huge amount of churn,
        # older state info might be valid from previous packs. I guess we
        # want to avoid backing these things up.
        stmt = """
        DELETE FROM object_refs_added
        WHERE zoid IN (
            SELECT zoid
            FROM pack_object
            WHERE keep = %(FALSE)s
        );

        DELETE FROM object_ref
        WHERE zoid IN (
            SELECT zoid
            FROM pack_object
            WHERE keep = %(FALSE)s
        );

        %(TRUNCATE)s pack_object
        """
        self.runner.run_script(cursor, stmt)
