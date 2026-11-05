# Signing trust bootstrap

A signature is accepted only when the signer identity and the issuer are
**exactly** the ones a policy names: a valid signature by somebody else is a
refusal, not a weaker pass. Cryptographic validity alone is not
authorization, and a signature attests origin and integrity of the signed
bytes -- never that any number in them is correct.

## What is signed today

`.github/workflows/supply-chain.yml` builds a release candidate from the
commit (`tools/supply_chain.py build`): the source archive, an SBOM derived
from `uv.lock`, an in-toto provenance statement, an index binding the commit
to the manifest's and the RO-Crate's digests, and `SHA256SUMS` over all of
them. Only the signing job holds `id-token: write`; it signs `SHA256SUMS`
keyless with that workflow's own GitHub OIDC identity, then
`tools/supply_chain.py verify` checks the signature online and offline and
every layer beneath it, and `tamper` requires every one-change copy to be
refused. `SUPPLY_CHAIN_THREAT_MODEL.md` lists what each layer stops.

## Where the trust comes from

`tools/supply_chain.py verify --policy P` takes the identity it trusts from a
policy file, and `load_policy` refuses a policy whose signer identity,
repository, workflow path or ref is empty, contains a wildcard or reads
PENDING; whose signer identity is not derived from its own repository,
workflow and ref; or whose issuer is not GitHub Actions' OIDC issuer.

The policy CI uses is `docs/supply_chain_ci_policy.json`, labelled
`CI_VALIDATION_ONLY`. It lives in the repository it describes, which is
acceptable for what it is -- a check that the signing path works on a
hosted runner -- and is exactly why it is **not** a release trust root: a
release cannot answer "whom should I trust?" from its own contents. A
release trust root would have to be supplied from outside the artifact and
authorized by the owner; none exists.

## Release status

No release is published, no tag is cut, and no release trust root is
defined. Publishing one needs the owner's separate authorization. No SLSA
build level is claimed: the provenance is written by the workflow it
describes.

## What was retired

The previous release verifier and its workflows (a release bundle of the
retired hardware-theory programme, its identity-discovery workflow and its
release policy) left the tree with that programme;
`docs/repository_inventory.json` records each file and why.
