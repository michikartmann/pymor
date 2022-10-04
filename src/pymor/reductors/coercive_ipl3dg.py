import numpy as np
from copy import deepcopy

from pymor.operators.constructions import ZeroOperator, LincombOperator, VectorOperator
from pymor.operators.constructions import VectorArrayOperator, IdentityOperator
from pymor.algorithms.projection import project
from pymor.operators.block import BlockOperator, BlockColumnOperator

from pymor.models.basic import StationaryModel
from pymor.reductors.coercive import CoerciveRBReductor
from pymor.reductors.basic import extend_basis
from pymor.algorithms.gram_schmidt import gram_schmidt

class CoerciveIPLD3GRBReductor(CoerciveRBReductor):
    def __init__(self, fom, dd_grid, local_bases=None):
        self.fom = fom
        self.solution_space = fom.solution_space
        self.S = self.solution_space.empty().num_blocks
        self._last_rom = None

        # TODO: assertions for local_bases
        self.local_bases = local_bases or [self.solution_space.empty().block(ss).empty()
                                           for ss in range(self.S)]

        # element patches for online enrichment
        element_patches = construct_element_patches(dd_grid)

        patch_models = []
        patch_mappings_to_global = []
        patch_mappings_to_local = []
        for element_patch in element_patches:
            patch_model, local_to_global_mapping, global_to_local_mapping = construct_patch_model(
                element_patch, fom.operator, fom.rhs, dd_grid.neighbors)
            patch_models.append(patch_model)
            patch_mappings_to_global.append(local_to_global_mapping)
            patch_mappings_to_local.append(global_to_local_mapping)

        self.element_patches = element_patches
        self.patch_models = patch_models
        self.patch_mappings_to_global = patch_mappings_to_global
        self.patch_mappings_to_local = patch_mappings_to_local

        # node patches for estimation
        # NOTE: currently the estimator domains are node patches
        inner_node_patches = construct_inner_node_patches(dd_grid)
        self.inner_node_patches = inner_node_patches
        estimator_domains = add_element_neighbors(dd_grid, inner_node_patches)
        self.estimator_domains = estimator_domains

    def add_global_solutions(self, us):
        assert us in self.fom.solution_space
        for I in range(self.S):
            us_block = us.block(I)
            try:
                extend_basis(us_block, self.local_bases[I])
            except:
                print('Extension failed')

    def add_local_solutions(self, I, u):
        try:
            extend_basis(u, self.local_bases[I])
        except:
            print('Extension failed')

    def enrich_all_locally(self, mu, use_global_matrix=False):
        for I in range(self.S):
            _ = self.enrich_locally(I, mu, use_global_matrix=use_global_matrix)

    def enrich_locally(self, I, mu, use_global_matrix=False):
        if self._last_rom is None:
            _ = self.reduce()
        mu_parsed = self.fom.parameters.parse(mu)
        current_solution = self._last_rom.solve(mu)
        mapping_to_global = self.patch_mappings_to_global[I]
        mapping_to_local = self.patch_mappings_to_local[I]
        patch_model = self.patch_models[I]

        if use_global_matrix:
            # use global matrix
            print("Warning: you are using the global operator here")

            # this part is using the global discretization
            current_solution_h = self.reconstruct(current_solution)
            a_u_v_global = self.fom.operator.apply(current_solution_h, mu_parsed)

            a_u_v_restricted_to_patch = []
            for i in range(len(patch_model.operator.range.subspaces)):
                i_global = mapping_to_global(i)
                a_u_v_restricted_to_patch.append(a_u_v_global.block(i_global))
            a_u_v_restricted_to_patch = patch_model.operator.range.make_array(
                a_u_v_restricted_to_patch)
            a_u_v_as_operator = VectorOperator(a_u_v_restricted_to_patch)
        else:
            u_restricted_to_patch = []
            for i_loc in range(len(patch_model.operator.range.subspaces)):
                i_global = mapping_to_global(i_loc)
                basis = self.reduced_local_bases[i_global]
                u_restricted_to_patch.append(
                    basis.lincomb(current_solution.block(i_global).to_numpy()))
            current_solution_on_patch = patch_model.operator.range.make_array(
                u_restricted_to_patch)

            new_op = remove_irrelevant_coupling_from_patch_operator(patch_model,
                                                                    mapping_to_global)
            a_u_v = new_op.apply(current_solution_on_patch, mu_parsed)
            a_u_v_as_operator = VectorOperator(a_u_v)

        patch_model_with_correction = patch_model.with_(
            rhs = patch_model.rhs - a_u_v_as_operator)
        # TODO: think about removing boundary dofs for the rhs
        phi = patch_model_with_correction.solve(mu)
        self.add_local_solutions(I, phi.block(mapping_to_local(I)))
        return phi

    def basis_length(self):
        return [len(self.local_bases[I]) for I in range(self.S)]

    def reduce(self, dims=None):
        assert dims is None, 'we cannot yet reduce to subbases'

        if self._last_rom is None or sum(self.basis_length()) > self._last_rom_dims:
            self._last_rom = self._reduce()
            self._last_rom_dims = sum(self.basis_length())
            # self.reduced_local_basis is required to perform multiple local enrichments
            # after the other
            # TODO: check how to fix this better
            self.reduced_local_bases = deepcopy(self.local_bases)

        return self._last_rom

    def project_operators(self):
        projected_ops_blocks = []
        # this is for BlockOperator(LincombOperators)
        assert isinstance(self.fom.operator, BlockOperator)

        # TODO: think about not projection the BlockOperator, but instead get rid
        # of the Block structure (like usual in localized MOR)
        # or use methodology of Stage 2 in TSRBLOD

        projected_operator = project_block_operator(self.fom.operator, self.local_bases,
                                                    self.local_bases)
        projected_rhs = project_block_rhs(self.fom.rhs, self.local_bases)

        projected_products = {k: project_block_operator(v, self.local_bases,
                                                        self.local_bases)
                              for k, v in self.fom.products.items()}

        # TODO: project output functional

        projected_operators = {
            'operator':          projected_operator,
            'rhs':               projected_rhs,
            'products':          projected_products,
            'output_functional': None
        }
        return projected_operators

    def assemble_error_estimator(self):
        return None

    def reduce_to_subbasis(self, dims):
        raise NotImplementedError

    def reconstruct(self, u_rom):
        u_ = []
        for I in range(self.S):
            basis = self.reduced_local_bases[I]
            u_I = u_rom.block(I)
            u_.append(basis.lincomb(u_I.to_numpy()))
        return self.fom.solution_space.make_array(u_)

    def from_patch_to_global(self, I, u_patch):
        # a function that construct a globally defined u from a patch u
        # only here for visualization purposes. not relevant for reductor
        u_global = []
        for i in range(self.S):
            i_loc = self.patch_mappings_to_local[I](i)
            if i_loc >= 0:
                u_global.append(u_patch.block(i_loc))
            else:
                # the multiplication with 1e-4 is only there for visualization
                # purposes. Otherwise, the colorbar gets scaled up until 1.
                # which is bad
                # TODO: fix colorscale for the plot
                u_global.append(self.solution_space.subspaces[i].ones()*1e-4)
        return self.solution_space.make_array(u_global)

def construct_patch_model(element_patch, block_op, block_rhs, neighbors):
    def local_to_global_mapping(i):
        return element_patch[i]
    def global_to_local_mapping(i):
        for i_, j in enumerate(element_patch):
            if j == i:
                return i_
        return -1

    S_patch = len(element_patch)
    patch_op = np.empty((S_patch, S_patch), dtype=object)
    patch_rhs = np.empty(S_patch, dtype=object)
    blocks_op = block_op.blocks
    blocks_rhs = block_rhs.blocks
    for ii in range(S_patch):
        I = local_to_global_mapping(ii)
        patch_op[ii][ii] = blocks_op[I][I]
        patch_rhs[ii] = blocks_rhs[I, 0]
        for J in neighbors(I):
            jj = global_to_local_mapping(J)
            if jj >= 0:
                # coupling contribution because nn is inside the patch
                patch_op[ii][jj] = blocks_op[I][J]
            else:
                # fake dirichlet contribution because nn is outside the patch
                # patch_op[ii][ii] += ops_dirichlet[ss][nn]
                pass

    final_patch_op = BlockOperator(patch_op)
    final_patch_rhs = BlockColumnOperator(patch_rhs)

    patch_model = StationaryModel(final_patch_op, final_patch_rhs)
    return patch_model, local_to_global_mapping, global_to_local_mapping

def construct_element_patches(dd_grid):
    # This is only working with quadrilateral meshes right now !
    # TODO: assert this
    element_patches = []
    for ss in range(dd_grid.num_subdomains):
        nh = {ss}
        nh.update(dd_grid.neighbors(ss))
        for nn in dd_grid.neighbors(ss):
            for nnn in dd_grid.neighbors(nn):
                if nnn not in nh and len(set(dd_grid.neighbors(nnn)).intersection(nh)) == 2:
                    nh.add(nnn)
        element_patches.append(tuple(nh))
    return element_patches

def construct_inner_node_patches(dd_grid):
    # This is only working with quadrilateral meshes right now !
    # TODO: assert this
    domain_ids = np.arange(dd_grid.num_subdomains).reshape(
        (int(np.sqrt(dd_grid.num_subdomains)),) * 2)
    node_patches = {}
    for i in range(domain_ids.shape[0] - 1):
        for j in range(domain_ids.shape[1] - 1):
            node_patches[i * domain_ids.shape[1] + j] = \
                    ([domain_ids[i, j], domain_ids[i, j+1],
                      domain_ids[i+1, j], domain_ids[i+1, j+1]])
    return node_patches

def add_element_neighbors(dd_grid, domains):
    new_domains = {}
    for (i, elements) in domains.items():
        elements_and_all_neighbors = []
        for el in elements:
            elements_and_all_neighbors.extend(dd_grid.neighbors(el))
        new_domains[i] = list(np.sort(np.unique(elements_and_all_neighbors)))
    return new_domains

def remove_irrelevant_coupling_from_patch_operator(patch_model, mapping_to_global):
    local_op = patch_model.operator
    subspaces = len(local_op.range.subspaces)
    ops_without_outside_coupling = np.empty((subspaces, subspaces), dtype=object)

    blocks = local_op.blocks
    for i in range(subspaces):
        for j in range(subspaces):
            if not i == j:
                if blocks[i][j]:
                    # the coupling stays
                    ops_without_outside_coupling[i][j] = blocks[i][j]
            else:
                # only the irrelevant couplings need to disappear
                if blocks[i][j]:
                    # only works for one thermal block right now
                    I = mapping_to_global(i)
                    element_patch = [mapping_to_global(l) for l in np.arange(subspaces)]
                    strings = []
                    for J in element_patch:
                        if I < J:
                            strings.append(f'{I}_{J}')
                        else:
                            strings.append(f'{J}_{I}')
                    local_ops, local_coefs = [], []
                    for op, coef in zip(blocks[i][j].operators, blocks[i][j].coefficients):
                        if ('volume' in op.name or 'boundary' in op.name or
                                     np.sum(string in op.name for string in strings)):
                            local_ops.append(op)
                            local_coefs.append(coef)

                    ops_without_outside_coupling[i][i] = LincombOperator(local_ops, local_coefs)

    return BlockOperator(ops_without_outside_coupling)

def project_block_operator(operator, range_bases, source_bases):
    # TODO: implement this with ruletables
    if isinstance(operator, LincombOperator):
        operators = []
        for op in operator.operators:
            operators.append(_project_block_operator(op, range_bases, source_bases))
        return LincombOperator(operators, operator.coefficients)
    else:
        return _project_block_operator(operator, range_bases, source_bases)

def _project_block_operator(operator, range_bases, source_bases):
    if isinstance(operator, IdentityOperator):
        local_projected_op = np.empty((len(range_bases), len(source_bases)), dtype=object)
        # TODO: assert that both bases are the same
        for I in range(len(range_bases)):
            local_projected_op[I][I] = project(IdentityOperator(range_bases[I].space),
                                               range_bases[I], range_bases[I])
    elif isinstance(operator, BlockOperator):
        local_projected_op = np.empty_like(operator.blocks)
        for I in range(len(range_bases)):
            local_basis_I = range_bases[I]
            for J in range(len(source_bases)):
                local_basis_J = source_bases[J]
                if operator.blocks[I][J]:
                    local_projected_op[I][J] = project(operator.blocks[I][J],
                                                       local_basis_I, local_basis_J)
    else:
        raise NotImplementedError
    projected_operator = BlockOperator(local_projected_op)
    return projected_operator

def project_block_rhs(rhs, range_bases):
    # TODO: implement this with ruletables
    if isinstance(rhs, LincombOperator):
        operators = []
        for op in rhs.operators:
            operators.append(_project_block_rhs(op, range_bases))
        return LincombOperator(operators, rhs.coefficients)
    else:
        return _project_block_rhs(rhs, range_bases)

def _project_block_rhs(rhs, range_bases):
    if isinstance(rhs, VectorOperator):
        rhs_blocks = rhs.array._blocks
        local_projected_rhs = np.empty(len(rhs_blocks), dtype=object)
        for I in range(len(range_bases)):
            rhs_operator = VectorArrayOperator(rhs_blocks[I])
            local_projected_rhs[I] = project(rhs_operator, range_bases[I], None)
    elif isinstance(rhs, BlockColumnOperator):
        local_projected_rhs = np.empty_like(rhs.blocks[:, 0])
        for I in range(len(range_bases)):
            local_projected_rhs[I] = project(rhs.blocks[I, 0], range_bases[I], None)
    else:
        raise NotImplementedError
    projected_rhs = BlockColumnOperator(local_projected_rhs)
    return projected_rhs