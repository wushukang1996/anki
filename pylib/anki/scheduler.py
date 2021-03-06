# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""
This file contains experimental scheduler changes, and is not currently
used by Anki.
"""

from __future__ import annotations

import pprint
import random
import time
from heapq import *
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import anki  # pylint: disable=unused-import
import anki._backend.backend_pb2 as _pb
from anki import hooks
from anki.cards import Card
from anki.consts import *
from anki.decks import Deck, DeckConfig, DeckManager, DeckTreeNode, QueueConfig
from anki.notes import Note
from anki.types import assert_exhaustive
from anki.utils import from_json_bytes, ids2str, intTime

CongratsInfo = _pb.CongratsInfoOut
CountsForDeckToday = _pb.CountsForDeckTodayOut
SchedTimingToday = _pb.SchedTimingTodayOut
UnburyCurrentDeck = _pb.UnburyCardsInCurrentDeckIn
BuryOrSuspend = _pb.BuryOrSuspendCardsIn


class Scheduler:
    _burySiblingsOnAnswer = True

    def __init__(self, col: anki.collection.Collection) -> None:
        self.col = col.weakref()
        self.queueLimit = 50
        self.reportLimit = 1000
        self.dynReportLimit = 99999
        self.reps = 0
        self.today: Optional[int] = None
        self._haveQueues = False
        self._lrnCutoff = 0
        self._updateCutoff()

    # Daily cutoff
    ##########################################################################

    def _updateCutoff(self) -> None:
        timing = self._timing_today()
        self.today = timing.days_elapsed
        self.dayCutoff = timing.next_day_at

    def _checkDay(self) -> None:
        # check if the day has rolled over
        if time.time() > self.dayCutoff:
            self.reset()

    def _timing_today(self) -> SchedTimingToday:
        return self.col._backend.sched_timing_today()

    # Fetching the next card
    ##########################################################################

    def reset(self) -> None:
        self.col.decks.update_active()
        self._updateCutoff()
        self._reset_counts()
        self._resetLrn()
        self._resetRev()
        self._resetNew()
        self._haveQueues = True

    def _reset_counts(self) -> None:
        tree = self.deck_due_tree(self.col.decks.selected())
        node = self.col.decks.find_deck_in_tree(tree, int(self.col.conf["curDeck"]))
        if not node:
            # current deck points to a missing deck
            self.newCount = 0
            self.revCount = 0
            self._immediate_learn_count = 0
        else:
            self.newCount = node.new_count
            self.revCount = node.review_count
            self._immediate_learn_count = node.learn_count

    def getCard(self) -> Optional[Card]:
        """Pop the next card from the queue. None if finished."""
        self._checkDay()
        if not self._haveQueues:
            self.reset()
        card = self._getCard()
        if card:
            self.col.log(card)
            if not self._burySiblingsOnAnswer:
                self._burySiblings(card)
            self.reps += 1
            card.startTimer()
            return card
        return None

    def _getCard(self) -> Optional[Card]:
        """Return the next due card, or None."""
        # learning card due?
        c = self._getLrnCard()
        if c:
            return c

        # new first, or time for one?
        if self._timeForNewCard():
            c = self._getNewCard()
            if c:
                return c

        # day learning first and card due?
        dayLearnFirst = self.col.conf.get("dayLearnFirst", False)
        if dayLearnFirst:
            c = self._getLrnDayCard()
            if c:
                return c

        # card due for review?
        c = self._getRevCard()
        if c:
            return c

        # day learning card due?
        if not dayLearnFirst:
            c = self._getLrnDayCard()
            if c:
                return c

        # new cards left?
        c = self._getNewCard()
        if c:
            return c

        # collapse or finish
        return self._getLrnCard(collapse=True)

    # Fetching new cards
    ##########################################################################

    def _resetNew(self) -> None:
        self._newDids = self.col.decks.active()[:]
        self._newQueue: List[int] = []
        self._updateNewCardRatio()

    def _fillNew(self, recursing: bool = False) -> bool:
        if self._newQueue:
            return True
        if not self.newCount:
            return False
        while self._newDids:
            did = self._newDids[0]
            lim = min(self.queueLimit, self._deckNewLimit(did))
            if lim:
                # fill the queue with the current did
                self._newQueue = self.col.db.list(
                    f"""
                select id from cards where did = ? and queue = {QUEUE_TYPE_NEW} order by due,ord limit ?""",
                    did,
                    lim,
                )
                if self._newQueue:
                    self._newQueue.reverse()
                    return True
            # nothing left in the deck; move to next
            self._newDids.pop(0)

        # if we didn't get a card but the count is non-zero,
        # we need to check again for any cards that were
        # removed from the queue but not buried
        if recursing:
            print("bug: fillNew()")
            return False
        self._reset_counts()
        self._resetNew()
        return self._fillNew(recursing=True)

    def _getNewCard(self) -> Optional[Card]:
        if self._fillNew():
            self.newCount -= 1
            return self.col.getCard(self._newQueue.pop())
        return None

    def _updateNewCardRatio(self) -> None:
        if self.col.conf["newSpread"] == NEW_CARDS_DISTRIBUTE:
            if self.newCount:
                self.newCardModulus = (self.newCount + self.revCount) // self.newCount
                # if there are cards to review, ensure modulo >= 2
                if self.revCount:
                    self.newCardModulus = max(2, self.newCardModulus)
                return
        self.newCardModulus = 0

    def _timeForNewCard(self) -> Optional[bool]:
        "True if it's time to display a new card when distributing."
        if not self.newCount:
            return False
        if self.col.conf["newSpread"] == NEW_CARDS_LAST:
            return False
        elif self.col.conf["newSpread"] == NEW_CARDS_FIRST:
            return True
        elif self.newCardModulus:
            return self.reps != 0 and self.reps % self.newCardModulus == 0
        else:
            # shouldn't reach
            return None

    def _deckNewLimit(
        self, did: int, fn: Optional[Callable[[Deck], int]] = None
    ) -> int:
        if not fn:
            fn = self._deckNewLimitSingle
        sel = self.col.decks.get(did)
        lim = -1
        # for the deck and each of its parents
        for g in [sel] + self.col.decks.parents(did):
            rem = fn(g)
            if lim == -1:
                lim = rem
            else:
                lim = min(rem, lim)
        return lim

    def _newForDeck(self, did: int, lim: int) -> int:
        "New count for a single deck."
        if not lim:
            return 0
        lim = min(lim, self.reportLimit)
        return self.col.db.scalar(
            f"""
select count() from
(select 1 from cards where did = ? and queue = {QUEUE_TYPE_NEW} limit ?)""",
            did,
            lim,
        )

    def _deckNewLimitSingle(self, g: DeckConfig) -> int:
        "Limit for deck without parent limits."
        if g["dyn"]:
            return self.dynReportLimit
        c = self.col.decks.confForDid(g["id"])
        limit = max(0, c["new"]["perDay"] - self.counts_for_deck_today(g["id"]).new)
        return hooks.scheduler_new_limit_for_single_deck(limit, g)

    def totalNewForCurrentDeck(self) -> int:
        return self.col.db.scalar(
            f"""
select count() from cards where id in (
select id from cards where did in %s and queue = {QUEUE_TYPE_NEW} limit ?)"""
            % self._deckLimit(),
            self.reportLimit,
        )

    # Fetching learning cards
    ##########################################################################

    # scan for any newly due learning cards every minute
    def _updateLrnCutoff(self, force: bool) -> bool:
        nextCutoff = intTime() + self.col.conf["collapseTime"]
        if nextCutoff - self._lrnCutoff > 60 or force:
            self._lrnCutoff = nextCutoff
            return True
        return False

    def _maybeResetLrn(self, force: bool) -> None:
        if self._updateLrnCutoff(force):
            self._resetLrn()

    def _resetLrnCount(self) -> None:
        # sub-day
        self.lrnCount = (
            self.col.db.scalar(
                f"""
select count() from cards where did in %s and queue = {QUEUE_TYPE_LRN}
and due < ?"""
                % (self._deckLimit()),
                self._lrnCutoff,
            )
            or 0
        )
        # day
        self.lrnCount += self.col.db.scalar(
            f"""
select count() from cards where did in %s and queue = {QUEUE_TYPE_DAY_LEARN_RELEARN}
and due <= ?"""
            % (self._deckLimit()),
            self.today,
        )
        # previews
        self.lrnCount += self.col.db.scalar(
            f"""
select count() from cards where did in %s and queue = {QUEUE_TYPE_PREVIEW}
"""
            % (self._deckLimit())
        )

    def _resetLrn(self) -> None:
        self._updateLrnCutoff(force=True)
        self._resetLrnCount()
        self._lrnQueue: List[Tuple[int, int]] = []
        self._lrnDayQueue: List[int] = []
        self._lrnDids = self.col.decks.active()[:]

    # sub-day learning
    def _fillLrn(self) -> Union[bool, List[Any]]:
        if not self.lrnCount:
            return False
        if self._lrnQueue:
            return True
        cutoff = intTime() + self.col.conf["collapseTime"]
        self._lrnQueue = self.col.db.all(  # type: ignore
            f"""
select due, id from cards where
did in %s and queue in ({QUEUE_TYPE_LRN},{QUEUE_TYPE_PREVIEW}) and due < ?
limit %d"""
            % (self._deckLimit(), self.reportLimit),
            cutoff,
        )
        for i in range(len(self._lrnQueue)):
            self._lrnQueue[i] = (self._lrnQueue[i][0], self._lrnQueue[i][1])
        # as it arrives sorted by did first, we need to sort it
        self._lrnQueue.sort()
        return self._lrnQueue

    def _getLrnCard(self, collapse: bool = False) -> Optional[Card]:
        self._maybeResetLrn(force=collapse and self.lrnCount == 0)
        if self._fillLrn():
            cutoff = time.time()
            if collapse:
                cutoff += self.col.conf["collapseTime"]
            if self._lrnQueue[0][0] < cutoff:
                id = heappop(self._lrnQueue)[1]
                card = self.col.getCard(id)
                self.lrnCount -= 1
                return card
        return None

    # daily learning
    def _fillLrnDay(self) -> Optional[bool]:
        if not self.lrnCount:
            return False
        if self._lrnDayQueue:
            return True
        while self._lrnDids:
            did = self._lrnDids[0]
            # fill the queue with the current did
            self._lrnDayQueue = self.col.db.list(
                f"""
select id from cards where
did = ? and queue = {QUEUE_TYPE_DAY_LEARN_RELEARN} and due <= ? limit ?""",
                did,
                self.today,
                self.queueLimit,
            )
            if self._lrnDayQueue:
                # order
                r = random.Random()
                r.seed(self.today)
                r.shuffle(self._lrnDayQueue)
                # is the current did empty?
                if len(self._lrnDayQueue) < self.queueLimit:
                    self._lrnDids.pop(0)
                return True
            # nothing left in the deck; move to next
            self._lrnDids.pop(0)
        # shouldn't reach here
        return False

    def _getLrnDayCard(self) -> Optional[Card]:
        if self._fillLrnDay():
            self.lrnCount -= 1
            return self.col.getCard(self._lrnDayQueue.pop())
        return None

    # Fetching reviews
    ##########################################################################

    def _currentRevLimit(self) -> int:
        d = self.col.decks.get(self.col.decks.selected(), default=False)
        return self._deckRevLimitSingle(d)

    def _deckRevLimitSingle(
        self, d: Dict[str, Any], parentLimit: Optional[int] = None
    ) -> int:
        # invalid deck selected?
        if not d:
            return 0

        if d["dyn"]:
            return self.dynReportLimit

        c = self.col.decks.confForDid(d["id"])
        lim = max(0, c["rev"]["perDay"] - self.counts_for_deck_today(d["id"]).review)

        if parentLimit is not None:
            lim = min(parentLimit, lim)
        elif "::" in d["name"]:
            for parent in self.col.decks.parents(d["id"]):
                # pass in dummy parentLimit so we don't do parent lookup again
                lim = min(lim, self._deckRevLimitSingle(parent, parentLimit=lim))
        return hooks.scheduler_review_limit_for_single_deck(lim, d)

    def _revForDeck(
        self, did: int, lim: int, childMap: DeckManager.childMapNode
    ) -> Any:
        dids = [did] + self.col.decks.childDids(did, childMap)
        lim = min(lim, self.reportLimit)
        return self.col.db.scalar(
            f"""
select count() from
(select 1 from cards where did in %s and queue = {QUEUE_TYPE_REV}
and due <= ? limit ?)"""
            % ids2str(dids),
            self.today,
            lim,
        )

    def _resetRev(self) -> None:
        self._revQueue: List[int] = []

    def _fillRev(self, recursing: bool = False) -> bool:
        "True if a review card can be fetched."
        if self._revQueue:
            return True
        if not self.revCount:
            return False

        lim = min(self.queueLimit, self._currentRevLimit())
        if lim:
            self._revQueue = self.col.db.list(
                f"""
select id from cards where
did in %s and queue = {QUEUE_TYPE_REV} and due <= ?
order by due, random()
limit ?"""
                % self._deckLimit(),
                self.today,
                lim,
            )

            if self._revQueue:
                # preserve order
                self._revQueue.reverse()
                return True

        if recursing:
            print("bug: fillRev2()")
            return False
        self._reset_counts()
        self._resetRev()
        return self._fillRev(recursing=True)

    def _getRevCard(self) -> Optional[Card]:
        if self._fillRev():
            self.revCount -= 1
            return self.col.getCard(self._revQueue.pop())
        return None

    # Answering a card
    ##########################################################################

    def answerCard(self, card: Card, ease: int) -> None:
        assert 1 <= ease <= 4
        assert 0 <= card.queue <= 4

        self.col.markReview(card)

        if self._burySiblingsOnAnswer:
            self._burySiblings(card)

        new_state = self._answerCard(card, ease)

        if not self._handle_leech(card, new_state):
            self._maybe_requeue_card(card)

    def _answerCard(self, card: Card, ease: int) -> _pb.SchedulingState:
        states = self.col._backend.get_next_card_states(card.id)
        if ease == BUTTON_ONE:
            new_state = states.again
            rating = _pb.AnswerCardIn.AGAIN
        elif ease == BUTTON_TWO:
            new_state = states.hard
            rating = _pb.AnswerCardIn.HARD
        elif ease == BUTTON_THREE:
            new_state = states.good
            rating = _pb.AnswerCardIn.GOOD
        elif ease == BUTTON_FOUR:
            new_state = states.easy
            rating = _pb.AnswerCardIn.EASY
        else:
            assert False, "invalid ease"

        self.col._backend.answer_card(
            card_id=card.id,
            current_state=states.current,
            new_state=new_state,
            rating=rating,
            answered_at_millis=intTime(1000),
            milliseconds_taken=card.timeTaken(),
        )

        # fixme: tests assume card will be mutated, so we need to reload it
        card.load()

        return new_state

    def _handle_leech(self, card: Card, new_state: _pb.SchedulingState) -> bool:
        "True if was leech."
        if self.col._backend.state_is_leech(new_state):
            if hooks.card_did_leech.count() > 0:
                hooks.card_did_leech(card)
                # leech hooks assumed that card mutations would be saved for them
                card.mod = intTime()
                card.usn = self.col.usn()
                card.flush()

            return True
        else:
            return False

    def _maybe_requeue_card(self, card: Card) -> None:
        # preview cards
        if card.queue == QUEUE_TYPE_PREVIEW:
            # adjust the count immediately, and rely on the once a minute
            # checks to requeue it
            self.lrnCount += 1
            return

        # learning cards
        if not card.queue == QUEUE_TYPE_LRN:
            return
        if card.due >= (intTime() + self.col.conf["collapseTime"]):
            return

        # card is due within collapse time, so we'll want to add it
        # back to the learning queue
        self.lrnCount += 1

        # if the queue is not empty and there's nothing else to do, make
        # sure we don't put it at the head of the queue and end up showing
        # it twice in a row
        if self._lrnQueue and not self.revCount and not self.newCount:
            smallestDue = self._lrnQueue[0][0]
            card.due = max(card.due, smallestDue + 1)

        heappush(self._lrnQueue, (card.due, card.id))

    def _cardConf(self, card: Card) -> DeckConfig:
        return self.col.decks.confForDid(card.did)

    def _home_config(self, card: Card) -> DeckConfig:
        return self.col.decks.confForDid(card.odid or card.did)

    def _deckLimit(self) -> str:
        return ids2str(self.col.decks.active())

    def counts_for_deck_today(self, deck_id: int) -> CountsForDeckToday:
        return self.col._backend.counts_for_deck_today(deck_id)

    # Next times
    ##########################################################################
    # fixme: move these into tests_schedv2 in the future

    def _interval_for_state(self, state: _pb.SchedulingState) -> int:
        kind = state.WhichOneof("value")
        if kind == "normal":
            return self._interval_for_normal_state(state.normal)
        elif kind == "filtered":
            return self._interval_for_filtered_state(state.filtered)
        else:
            assert_exhaustive(kind)
            return 0  # unreachable

    def _interval_for_normal_state(self, normal: _pb.SchedulingState.Normal) -> int:
        kind = normal.WhichOneof("value")
        if kind == "new":
            return 0
        elif kind == "review":
            return normal.review.scheduled_days * 86400
        elif kind == "learning":
            return normal.learning.scheduled_secs
        elif kind == "relearning":
            return normal.relearning.learning.scheduled_secs
        else:
            assert_exhaustive(kind)
            return 0  # unreachable

    def _interval_for_filtered_state(
        self, filtered: _pb.SchedulingState.Filtered
    ) -> int:
        kind = filtered.WhichOneof("value")
        if kind == "preview":
            return filtered.preview.scheduled_secs
        elif kind == "rescheduling":
            return self._interval_for_normal_state(filtered.rescheduling.original_state)
        else:
            assert_exhaustive(kind)
            return 0  # unreachable

    def nextIvl(self, card: Card, ease: int) -> Any:
        "Don't use this - it is only required by tests, and will be moved in the future."
        states = self.col._backend.get_next_card_states(card.id)
        if ease == BUTTON_ONE:
            new_state = states.again
        elif ease == BUTTON_TWO:
            new_state = states.hard
        elif ease == BUTTON_THREE:
            new_state = states.good
        elif ease == BUTTON_FOUR:
            new_state = states.easy
        else:
            assert False, "invalid ease"

        return self._interval_for_state(new_state)

    # Sibling spacing
    ##########################################################################

    def _burySiblings(self, card: Card) -> None:
        toBury: List[int] = []
        conf = self._home_config(card)
        bury_new = conf["new"].get("bury", True)
        bury_rev = conf["rev"].get("bury", True)
        # loop through and remove from queues
        for cid, queue in self.col.db.execute(
            f"""
select id, queue from cards where nid=? and id!=?
and (queue={QUEUE_TYPE_NEW} or (queue={QUEUE_TYPE_REV} and due<=?))""",
            card.nid,
            card.id,
            self.today,
        ):
            if queue == QUEUE_TYPE_REV:
                queue_obj = self._revQueue
                if bury_rev:
                    toBury.append(cid)
            else:
                queue_obj = self._newQueue
                if bury_new:
                    toBury.append(cid)

            # even if burying disabled, we still discard to give same-day spacing
            try:
                queue_obj.remove(cid)
            except ValueError:
                pass
        # then bury
        if toBury:
            self.bury_cards(toBury, manual=False)

    # Review-related UI helpers
    ##########################################################################

    def counts(self, card: Optional[Card] = None) -> Tuple[int, int, int]:
        counts = [self.newCount, self.lrnCount, self.revCount]
        if card:
            idx = self.countIdx(card)
            counts[idx] += 1
        new, lrn, rev = counts
        return (new, lrn, rev)

    def countIdx(self, card: Card) -> int:
        if card.queue in (QUEUE_TYPE_DAY_LEARN_RELEARN, QUEUE_TYPE_PREVIEW):
            return QUEUE_TYPE_LRN
        return card.queue

    def answerButtons(self, card: Card) -> int:
        conf = self._cardConf(card)
        if card.odid and not conf["resched"]:
            return 2
        return 4

    def nextIvlStr(self, card: Card, ease: int, short: bool = False) -> str:
        "Return the next interval for CARD as a string."
        states = self.col._backend.get_next_card_states(card.id)
        return self.col._backend.describe_next_states(states)[ease - 1]

    # Deck list
    ##########################################################################

    def deck_due_tree(self, top_deck_id: int = 0) -> DeckTreeNode:
        """Returns a tree of decks with counts.
        If top_deck_id provided, counts are limited to that node."""
        return self.col._backend.deck_tree(top_deck_id=top_deck_id, now=intTime())

    # Deck finished state & custom study
    ##########################################################################

    def congratulations_info(self) -> CongratsInfo:
        return self.col._backend.congrats_info()

    def haveBuriedSiblings(self) -> bool:
        return self.congratulations_info().have_sched_buried

    def haveManuallyBuried(self) -> bool:
        return self.congratulations_info().have_user_buried

    def haveBuried(self) -> bool:
        info = self.congratulations_info()
        return info.have_sched_buried or info.have_user_buried

    def extendLimits(self, new: int, rev: int) -> None:
        did = self.col.decks.current()["id"]
        self.col._backend.extend_limits(deck_id=did, new_delta=new, review_delta=rev)

    def _is_finished(self) -> bool:
        "Don't use this, it is a stop-gap until this code is refactored."
        return not any((self.newCount, self.revCount, self._immediate_learn_count))

    def totalRevForCurrentDeck(self) -> int:
        return self.col.db.scalar(
            f"""
select count() from cards where id in (
select id from cards where did in %s and queue = {QUEUE_TYPE_REV} and due <= ? limit ?)"""
            % self._deckLimit(),
            self.today,
            self.reportLimit,
        )

    # Filtered deck handling
    ##########################################################################

    def rebuild_filtered_deck(self, deck_id: int) -> int:
        return self.col._backend.rebuild_filtered_deck(deck_id)

    def empty_filtered_deck(self, deck_id: int) -> None:
        self.col._backend.empty_filtered_deck(deck_id)

    # Suspending & burying
    ##########################################################################

    def unsuspend_cards(self, ids: List[int]) -> None:
        self.col._backend.restore_buried_and_suspended_cards(ids)

    def unbury_cards(self, ids: List[int]) -> None:
        self.col._backend.restore_buried_and_suspended_cards(ids)

    def unbury_cards_in_current_deck(
        self,
        mode: UnburyCurrentDeck.Mode.V = UnburyCurrentDeck.ALL,
    ) -> None:
        self.col._backend.unbury_cards_in_current_deck(mode)

    def suspend_cards(self, ids: Sequence[int]) -> None:
        self.col._backend.bury_or_suspend_cards(
            card_ids=ids, mode=BuryOrSuspend.SUSPEND
        )

    def bury_cards(self, ids: Sequence[int], manual: bool = True) -> None:
        if manual:
            mode = BuryOrSuspend.BURY_USER
        else:
            mode = BuryOrSuspend.BURY_SCHED
        self.col._backend.bury_or_suspend_cards(card_ids=ids, mode=mode)

    def bury_note(self, note: Note) -> None:
        self.bury_cards(note.card_ids())

    # Resetting/rescheduling
    ##########################################################################

    def schedule_cards_as_new(self, card_ids: List[int]) -> None:
        "Put cards at the end of the new queue."
        self.col._backend.schedule_cards_as_new(card_ids=card_ids, log=True)

    def set_due_date(self, card_ids: List[int], days: str) -> None:
        """Set cards to be due in `days`, turning them into review cards if necessary.
        `days` can be of the form '5' or '5..7'"""
        self.col._backend.set_due_date(card_ids=card_ids, days=days)

    def resetCards(self, ids: List[int]) -> None:
        "Completely reset cards for export."
        sids = ids2str(ids)
        # we want to avoid resetting due number of existing new cards on export
        nonNew = self.col.db.list(
            f"select id from cards where id in %s and (queue != {QUEUE_TYPE_NEW} or type != {CARD_TYPE_NEW})"
            % sids
        )
        # reset all cards
        self.col.db.execute(
            f"update cards set reps=0,lapses=0,odid=0,odue=0,queue={QUEUE_TYPE_NEW}"
            " where id in %s" % sids
        )
        # and forget any non-new cards, changing their due numbers
        self.col._backend.schedule_cards_as_new(card_ids=nonNew, log=False)

    # Repositioning new cards
    ##########################################################################

    def sortCards(
        self,
        cids: List[int],
        start: int = 1,
        step: int = 1,
        shuffle: bool = False,
        shift: bool = False,
    ) -> None:
        self.col._backend.sort_cards(
            card_ids=cids,
            starting_from=start,
            step_size=step,
            randomize=shuffle,
            shift_existing=shift,
        )

    def randomizeCards(self, did: int) -> None:
        self.col._backend.sort_deck(deck_id=did, randomize=True)

    def orderCards(self, did: int) -> None:
        self.col._backend.sort_deck(deck_id=did, randomize=False)

    def resortConf(self, conf: DeckConfig) -> None:
        for did in self.col.decks.didsForConf(conf):
            if conf["new"]["order"] == 0:
                self.randomizeCards(did)
            else:
                self.orderCards(did)

    # for post-import
    def maybeRandomizeDeck(self, did: Optional[int] = None) -> None:
        if not did:
            did = self.col.decks.selected()
        conf = self.col.decks.confForDid(did)
        # in order due?
        if conf["new"]["order"] == NEW_CARDS_RANDOM:
            self.randomizeCards(did)

    ##########################################################################

    def __repr__(self) -> str:
        d = dict(self.__dict__)
        del d["col"]
        return f"{super().__repr__()} {pprint.pformat(d, width=300)}"

    # unit tests
    def _fuzzIvlRange(self, ivl: int) -> Tuple[int, int]:
        return (ivl, ivl)

    # Legacy aliases and helpers
    ##########################################################################

    def reschedCards(
        self, card_ids: List[int], min_interval: int, max_interval: int
    ) -> None:
        self.set_due_date(card_ids, f"{min_interval}-{max_interval}!")

    def buryNote(self, nid: int) -> None:
        note = self.col.getNote(nid)
        self.bury_cards(note.card_ids())

    def unburyCards(self) -> None:
        print(
            "please use unbury_cards() or unbury_cards_in_current_deck instead of unburyCards()"
        )
        self.unbury_cards_in_current_deck()

    def unburyCardsForDeck(self, type: str = "all") -> None:
        print(
            "please use unbury_cards_in_current_deck() instead of unburyCardsForDeck()"
        )
        if type == "all":
            mode = UnburyCurrentDeck.ALL
        elif type == "manual":
            mode = UnburyCurrentDeck.USER_ONLY
        else:  # elif type == "siblings":
            mode = UnburyCurrentDeck.SCHED_ONLY
        self.unbury_cards_in_current_deck(mode)

    def finishedMsg(self) -> str:
        print("finishedMsg() is obsolete")
        return ""

    def _nextDueMsg(self) -> str:
        print("_nextDueMsg() is obsolete")
        return ""

    def rebuildDyn(self, did: Optional[int] = None) -> Optional[int]:
        did = did or self.col.decks.selected()
        count = self.rebuild_filtered_deck(did) or None
        if not count:
            return None
        # and change to our new deck
        self.col.decks.select(did)
        return count

    def emptyDyn(self, did: Optional[int], lim: Optional[str] = None) -> None:
        if lim is None:
            self.empty_filtered_deck(did)
            return

        queue = f"""
queue = (case when queue < 0 then queue
              when type in (1,{CARD_TYPE_RELEARNING}) then
  (case when (case when odue then odue else due end) > 1000000000 then 1 else
  {QUEUE_TYPE_DAY_LEARN_RELEARN} end)
else
  type
end)
"""
        self.col.db.execute(
            """
update cards set did = odid, %s,
due = (case when odue>0 then odue else due end), odue = 0, odid = 0, usn = ? where %s"""
            % (queue, lim),
            self.col.usn(),
        )

    def remFromDyn(self, cids: List[int]) -> None:
        self.emptyDyn(None, f"id in {ids2str(cids)} and odid")

    def update_stats(
        self,
        deck_id: int,
        new_delta: int = 0,
        review_delta: int = 0,
        milliseconds_delta: int = 0,
    ) -> None:
        self.col._backend.update_stats(
            deck_id=deck_id,
            new_delta=new_delta,
            review_delta=review_delta,
            millisecond_delta=milliseconds_delta,
        )

    def _updateStats(self, card: Card, type: str, cnt: int = 1) -> None:
        did = card.did
        if type == "new":
            self.update_stats(did, new_delta=cnt)
        elif type == "rev":
            self.update_stats(did, review_delta=cnt)
        elif type == "time":
            self.update_stats(did, milliseconds_delta=cnt)

    def deckDueTree(self) -> List:
        "List of (base name, did, rev, lrn, new, children)"
        print(
            "deckDueTree() is deprecated; use decks.deck_tree() for a tree without counts, or sched.deck_due_tree()"
        )
        return from_json_bytes(self.col._backend.deck_tree_legacy())[5]

    def _newConf(self, card: Card) -> QueueConfig:
        return self._home_config(card)["new"]

    def _lapseConf(self, card: Card) -> QueueConfig:
        return self._home_config(card)["lapse"]

    def _revConf(self, card: Card) -> QueueConfig:
        return self._home_config(card)["rev"]

    def _lrnConf(self, card: Card) -> QueueConfig:
        if card.type in (CARD_TYPE_REV, CARD_TYPE_RELEARNING):
            return self._lapseConf(card)
        else:
            return self._newConf(card)

    unsuspendCards = unsuspend_cards
    buryCards = bury_cards
    suspendCards = suspend_cards
    forgetCards = schedule_cards_as_new
