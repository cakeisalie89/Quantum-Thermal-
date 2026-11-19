---------------------------- MODULE WitnessQuorum ----------------------------
(***************************************************************************)
(* A quorum of tree witnesses against a split view (Phase E, protocol 7).  *)
(*                                                                         *)
(* agent_substrate.witness.TreeWitness holds the last tree it cosigned and *)
(* cosigns a new one only when the one it holds is a prefix of it (a       *)
(* consistency proof). A witness that lost its state refuses everything    *)
(* until the owner provisions it again, at the first record's tree.        *)
(* verify_tree_witnessed counts a tree as witnessed when a quorum of the   *)
(* trust root's witnesses cosigned it.                                     *)
(*                                                                         *)
(* THE ADVERSARY runs the log: it shows each witness whatever history it   *)
(* likes, and tries to get two histories that fork witnessed by a quorum   *)
(* each, so that two readers accept two different pasts. It also controls  *)
(* up to F witnesses outright: a faulty witness cosigns anything.          *)
(*                                                                         *)
(* THE CLAIM. Every two trees a quorum has EVER witnessed are consistent,  *)
(* one a prefix of the other, when two quorums share more witnesses than   *)
(* can be faulty: 2 * Quorum - N > F. A witness that lost its state and    *)
(* was provisioned again remembers nothing of what it cosigned before, so  *)
(* it is counted against F: provisioning it again does not make it honest. *)
(*                                                                         *)
(* NOT MODELLED: signatures (a cosignature is the witness's and nobody     *)
(* else's), hashes (a tree is its sequence of records: equal roots are     *)
(* equal sequences), time and staleness, and the readers themselves: a     *)
(* reader that accepts a tree a quorum witnessed sees what this model      *)
(* calls witnessed.                                                        *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Witnesses, F, Quorum, SmallQuorum, Values, MaxLen, Bug

ASSUME Bug \in {"none", "small_quorum", "no_consistency", "loss_uncounted"}
ASSUME F \in Nat /\ Quorum \in Nat /\ SmallQuorum \in Nat /\ MaxLen \in Nat

Genesis == <<"g">>

\* Every tree a history can have: the first record, then up to MaxLen - 1
\* more, each one of Values.
Trees == {Genesis \o s : s \in UNION {[1..k -> Values] : k \in 0..(MaxLen - 1)}}

IsPrefix(p, q) == Len(p) <= Len(q) /\ SubSeq(q, 1, Len(p)) = p
Consistent(p, q) == IsPrefix(p, q) \/ IsPrefix(q, p)

Q == IF Bug = "small_quorum" THEN SmallQuorum ELSE Quorum

\* <<>> is "holds no tree": the state was lost.
VARIABLES held, cosigned, faulty, witnessed, last

vars == <<held, cosigned, faulty, witnessed, last>>

\* Who vouches for t: each witness not faulty that cosigned it, and every
\* faulty witness, which cosigns whatever it is asked to.
Vouchers(c, f, t) == {w \in Witnesses \ f : t \in c[w]} \cup f
NowWitnessed(c, f) == {t \in Trees : Cardinality(Vouchers(c, f, t)) >= Q}

Init ==
  /\ held = [w \in Witnesses |-> Genesis]
  /\ cosigned = [w \in Witnesses |-> {Genesis}]
  /\ faulty = {}
  /\ witnessed = NowWitnessed([w \in Witnesses |-> {Genesis}], {})
  /\ last = <<"Init">>

\* What a witness accepts, shown t with a consistency proof from what it
\* holds when there is one.
Accepts(w, t) ==
  IF Bug = "no_consistency" THEN Len(held[w]) <= Len(t)
                            ELSE IsPrefix(held[w], t)

Cosign(w, t) ==
  /\ w \notin faulty /\ held[w] # <<>>
  /\ Accepts(w, t)
  /\ held' = [held EXCEPT ![w] = t]
  /\ cosigned' = [cosigned EXCEPT ![w] = @ \cup {t}]
  /\ witnessed' = witnessed \cup NowWitnessed(cosigned', faulty)
  /\ last' = <<"Cosign", w, t>>
  /\ UNCHANGED faulty

\* The adversary takes a witness over.
Corrupt(w) ==
  /\ w \notin faulty /\ Cardinality(faulty) < F
  /\ faulty' = faulty \cup {w}
  /\ witnessed' = witnessed \cup NowWitnessed(cosigned, faulty')
  /\ last' = <<"Corrupt", w>>
  /\ UNCHANGED <<held, cosigned>>

\* A witness loses its state. It is a fault from then on, whether it keeps
\* its key or not -- unless the protocol forgets to count it.
Lose(w) ==
  /\ w \notin faulty /\ held[w] # <<>>
  /\ Bug = "loss_uncounted" \/ Cardinality(faulty) < F
  /\ held' = [held EXCEPT ![w] = <<>>]
  /\ faulty' = IF Bug = "loss_uncounted" THEN faulty ELSE faulty \cup {w}
  /\ witnessed' = witnessed \cup NowWitnessed(cosigned, faulty')
  /\ last' = <<"Lose", w>>
  /\ UNCHANGED cosigned

\* The owner provisions it again, at the first record's tree.
Reprovision(w) ==
  /\ held[w] = <<>>
  /\ held' = [held EXCEPT ![w] = Genesis]
  /\ last' = <<"Reprovision", w>>
  /\ UNCHANGED <<cosigned, faulty, witnessed>>

Next ==
  \E w \in Witnesses :
    \/ \E t \in Trees : Cosign(w, t)
    \/ Corrupt(w) \/ Lose(w) \/ Reprovision(w)

Spec == Init /\ [][Next]_vars

TypeOK ==
  /\ held \in [Witnesses -> Trees \cup {<<>>}]
  /\ cosigned \in [Witnesses -> SUBSET Trees]
  /\ faulty \subseteq Witnesses /\ witnessed \subseteq Trees

\* SAFETY: no split view. Every two trees a quorum has ever witnessed are
\* consistent.
NoSplitView == \A t1, t2 \in witnessed : Consistent(t1, t2)

=============================================================================
