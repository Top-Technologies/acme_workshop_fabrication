from markupsafe import Markup

from odoo import fields, models, _
from odoo.exceptions import UserError
from odoo.addons.acme_project_core.models.boq_revision import _BOQ_TOKEN


class ProjectSourcing(models.Model):
    _inherit = 'project.project'

    def action_acme_review_sourcing(self):
        self.ensure_one()
        if not self.acme_can_confirm_boq:
            self._acme_check_preparation_editor(allow_dm=True)
        self.check_access('read')
        return self._acme_sourcing_review_action()

    def _acme_sourcing_review_action(self):
        return {
            'type': 'ir.actions.act_window', 'name': _('Effective BOQ Sourcing'),
            'res_model': 'acme.project.boq', 'view_mode': 'list,form',
            'views': [(self.env.ref('acme_workshop_fabrication.sourcing_boq_list').id, 'list'),
                      (self.env.ref('acme_workshop_fabrication.sourcing_boq_form').id, 'form')],
            'domain': [('project_id', '=', self.id)],
        }


class ProjectBoqSourcing(models.Model):
    _inherit = 'acme.project.boq'

    acme_sourcing_route_id = fields.Many2one(
        'stock.route', string='Project Line Route Override', check_company=True, ondelete='restrict',
        help='Overrides the replenishment route for this BOQ line without changing the product defaults.',
    )
    acme_sourcing_fabrication_line_id = fields.Many2one(
        'acme.fabrication.line', string='Project BoM Source', check_company=True, ondelete='restrict',
    )
    acme_sourcing_preview = fields.Html(compute='_compute_acme_sourcing_preview')

    def write(self, vals):
        settings = {'acme_sourcing_route_id', 'acme_sourcing_fabrication_line_id'}
        if vals and set(vals) <= settings:
            self.project_id._acme_check_preparation_editor(allow_dm=True)
            result = super(ProjectBoqSourcing, self.with_context(acme_boq_token=_BOQ_TOKEN)).write(vals)
            for line in self.filtered('acme_sourcing_fabrication_line_id'):
                source = line.acme_sourcing_fabrication_line_id
                if source.request_id.project_id != line.project_id or not source.bom_id or source.acme_boq_demand_id:
                    raise UserError(_('Select a fabrication line with a prepared BoM from this project.'))
            return result
        return super().write(vals)

    def _acme_component_demand(self, quantity=None):
        self.ensure_one()
        quantity = self.quantity if quantity is None else quantity
        order_line = self.source_line_id
        product = order_line.product_id
        if not product:
            raise UserError(_('Link this BOQ item to a Sales Order line with a product and unit.'))
        if not quantity:
            return {}
        route = self.acme_sourcing_route_id
        source = self.acme_sourcing_fabrication_line_id
        if not source:
            sources = self.env['acme.fabrication.line'].search([
                ('request_id.project_id', '=', self.project_id.id), ('product_id', '=', product.id),
                ('bom_id', '!=', False), ('acme_boq_demand_id', '=', False),
            ])
            if len(sources) > 1:
                raise UserError(_('Several project BoMs match this item. Select its fabrication source.'))
            source = sources
        root = source.bom_id
        if root and (not root.active or source.request_id.state not in ('received', 'reported', 'accepted')):
            raise UserError(_('Select a current project BoM from a fabrication request received by Workshop.'))
        if root and root.product_id and root.product_id != product and product.type != 'service':
            raise UserError(_('The selected project BoM does not match the Sales Order product.'))
        if not root:
            root = self._acme_kit(product)
        if not root:
            if product.type == 'service':
                raise UserError(_('The commercial parent has no applicable project BoM. Select a fabrication source.'))
            return {product: order_line.product_uom_id._compute_quantity(quantity, product.uom_id)}
        if root.product_id and route.rule_ids.filtered(lambda rule: rule.action == 'manufacture'):
            output = root.product_id
            return {output: order_line.product_uom_id._compute_quantity(quantity, output.uom_id, round=False)}
        demand = {}
        self._acme_expand_demand(root, root.product_id or product, order_line.product_uom_id,
                                 quantity, demand, set())
        return demand

    def _acme_kit(self, product):
        kits = self.env['mrp.bom'].search([
            ('type', '=', 'phantom'), ('acme_request_line_id', '=', False),
            ('company_id', 'in', [False, self.company_id.id]),
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
            ('product_id', 'in', [False, product.id]),
        ])
        if len(kits) > 1:
            raise UserError(_('Multiple applicable kit BoMs exist for %s.', product.display_name))
        return kits

    def _acme_expand_demand(self, bom, variant, uom, quantity, demand, parents):
        if bom.id in parents or not bom.product_qty:
            raise UserError(_('The BoM is recursive or has no positive batch quantity.'))
        factor = uom._compute_quantity(quantity, bom.product_uom_id, round=False) / bom.product_qty
        for part in bom.bom_line_ids:
            if part._skip_bom_line(variant):
                continue
            product = part.product_id
            qty = factor * part.product_qty
            routes = product.route_ids | product.categ_id.total_route_ids
            routed = bool(routes.rule_ids.filtered(lambda rule: rule.action in ('buy', 'manufacture')))
            kit = self.env['mrp.bom'] if routed else self._acme_kit(product)
            if kit:
                self._acme_expand_demand(kit, product, part.product_uom_id, qty, demand, parents | {bom.id})
            else:
                amount = part.product_uom_id._compute_quantity(qty, product.uom_id, round=False)
                demand[product] = demand.get(product, 0) + amount

    def _compute_acme_sourcing_preview(self):
        for line in self:
            try:
                demand = line._acme_component_demand()
            except UserError as error:
                line.acme_sourcing_preview = Markup('<p>%s</p>') % str(error)
                continue
            warehouse = line.source_line_id.warehouse_id
            if not warehouse:
                line.acme_sourcing_preview = Markup('<p>%s</p>') % _('Select a Sales Order warehouse to review availability.')
                continue
            rows = []
            linked = line.acme_demand_move_ids
            products = set(demand) | set(linked.product_id | line.acme_demand_fabrication_line_ids.product_id | line.acme_direct_purchase_ids.order_line.product_id)
            for product in sorted(products, key=lambda item: item.id):
                quantity = demand.get(product, 0)
                stock = product.with_company(line.company_id).with_context(warehouse_id=warehouse.id)
                quantity = product.uom_id.round(quantity, rounding_method='UP')
                coverage = [line._acme_move_coverage(move) for move in linked if move.product_id == product]
                staged, reserved, incoming = (sum(row[index] for row in coverage) for index in range(3))
                purchases = line.acme_direct_purchase_ids.filtered(lambda order: order.state != 'cancel').order_line
                open_purchases = purchases.filtered(lambda item: item.product_id == product and item.product_qty > item.qty_received)
                incoming += sum(item.product_uom_id._compute_quantity(item.product_qty - item.qty_received,
                                                                      product.uom_id, round=False)
                                for item in open_purchases)
                dates = sorted({date for row in coverage for date in row[3]} | set(open_purchases.mapped('date_planned')) - {False})
                rows.append(Markup('<tr><td>%s</td><td>%s %s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>') % (
                    product.display_name, quantity, product.uom_id.name, staged, reserved, incoming, line._acme_fabrication_coverage(product),
                    ', '.join(str(date.date()) for date in dates), stock.qty_available,
                    stock.qty_available - stock.free_qty, stock.incoming_qty,
                ))
            labels = [_('Material'), _('Required'), _('Project Staged'), _('Project Reserved'),
                      _('Project Incoming / RFQ'), _('Fabrication Requested'), _('Expected Dates'), _('Warehouse On Hand'),
                      _('Warehouse Reserved'), _('Warehouse Incoming')]
            headings = Markup('').join(Markup('<th>%s</th>') % label for label in labels)
            line.acme_sourcing_preview = Markup('<table class="table table-sm"><thead><tr>%s</tr></thead><tbody>%s</tbody></table>') % (
                headings, Markup('').join(rows),
            )

    def _acme_open_sourcing_documents(self, documents, title):
        self.ensure_one()
        self.check_access('read')
        documents.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': title, 'res_model': documents._name,
                'view_mode': 'list,form', 'domain': [('id', 'in', documents.ids)]}

    def action_acme_sourcing_rfqs(self):
        return self._acme_open_sourcing_documents(self.acme_sourcing_rfqs, _('Purchases / RFQs'))

    def action_acme_sourcing_transfers(self):
        return self._acme_open_sourcing_documents(self.acme_sourcing_transfers, _('Transfers'))

    def action_acme_sourcing_fabrication(self):
        return self._acme_open_sourcing_documents(self.acme_sourcing_fabrication_requests, _('Fabrication Requests'))

    def action_acme_sourcing_forecast(self):
        self.ensure_one()
        if not self.project_id.acme_can_confirm_boq:
            self.project_id._acme_check_preparation_editor(allow_dm=True)
        self.check_access('read')
        products = self.env['product.product'].browse([product.id for product in self._acme_component_demand()])
        if len(products) == 1:
            return products.with_context(warehouse_id=self.source_line_id.warehouse_id.id).action_product_forecast_report()
        return {
            'type': 'ir.actions.act_window', 'name': _('Demand Products — Open Product Forecast for Expected Dates'),
            'res_model': 'product.product', 'view_mode': 'list,form',
            'domain': [('id', 'in', products.ids)],
            'context': {'warehouse_id': self.source_line_id.warehouse_id.id},
        }
