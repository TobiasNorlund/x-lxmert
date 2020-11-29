import numpy as np
import torch
import torch.nn as nn

from transformers.modeling_lxmert import LxmertModel, LxmertPreTrainedModel, LxmertVisualObjHead

class ImggenModel(LxmertPreTrainedModel):
    def __init__(self, config, args, num_clusters=10000):
        super().__init__(config)

        self.config = config
        self.args = args
        self.config.num_clusters = num_clusters
        self.config.clustering = num_clusters > 0

        self.bert = LxmertModel(config)

        self.obj_predict_head = LxmertVisualObjHead(config)

        self.mask_feat = nn.Parameter(torch.zeros(config.visual_feat_dim))
        self.vis_emb = None

    def set_visual_embedding(self, centroids):
        self.vis_emb = nn.Embedding.from_pretrained(
            torch.from_numpy(centroids),
            freeze=True
        )
        if self.task_obj_predict:


    def forward(self, visn_feats, sent_feats, label_dict=None,
                task=['word_mask', 'vis_mask', 'matched'], calc_loss=True, vis_AR=False, G_input_feed=False):
        """
        Args:
            - visn_feats (tuple)
                code                   B, n_grids, codebook_dim
                grid                   B, n_grids, 4
                grid_mask              B, n_grids     (1: [MASK], 0: no mask)
            - sent_feats  (tuple)
                word_id:               B, max_text_length
                token_type_ids:        B, max_text_length
                word_attention_mask:   B, max_text_length
            - label_dict
                code_labels            B, n_grids            (-1 => no MASK => no loss)
                word_labels            B, max_text_length    (-1 => no MASK => no loss)
                matched_labels         B

        Return:
            out_dict
                'code_loss'
                'word_loss'
                'matched_loss'
        """
        B = sent_feats[0].size(0)

        code, grid, grid_mask = visn_feats

        # Masking visual feats
        if task == 'vis_mask':
            code = torch.where(grid_mask.view(B, self.args.n_grids, 1).bool(),
                               self.mask_feat.view(1, 1, -1).to(dtype=code.dtype),
                               code)

        word_id, token_type_ids, word_attention_mask = sent_feats

        # print('input word id')
        # print(word_id)
        # print('input word attention mask')
        # print(word_attention_mask.long())

        (lang_output, visn_output), pooled_output = self.bert(
            word_id, token_type_ids, word_attention_mask,
            visual_feats=(code, grid),
            skip_vis_fc=False,
            get_lang_cls_feat=False,
            visual_AR=vis_AR)

        # assert lang_output.size() == (B, self.args.max_text_length, self.config.hidden_size), lang_output.size()
        if self.args.grid_model:
            assert visn_output.size() == (B, self.args.n_grids, self.config.hidden_size), visn_output.size()
        else:
            assert visn_output.size() == (B, self.args.n_boxes, self.config.hidden_size), visn_output.size()

        out_dict = {}

        if task in ['word_mask', 'matched']:
            lang_prediction_scores, cross_relationship_score = self.cls(
                lang_output, pooled_output)

        if task == 'vis_mask':
            # [B, n_grids, n_codebook]
            vis_head_out = self.obj_predict_head(visn_output, ['obj', 'feat'])

            code_logit = vis_head_out['obj']
            regressed_code = vis_head_out['feat']

            # code_logit = self.obj_predict_head(visn_output, ['obj'])['obj']
            assert code_logit.size() == (B, self.args.n_grids, self.args.n_codebook), code_logit.size()

            if calc_loss:
                code_label = label_dict['code_labels']
                out_dict['code_loss'] = self.cross_entropy(
                    code_logit.view(B*self.args.n_grids, self.args.n_codebook),
                    code_label.flatten()
                )

                if G_input_feed:
                    pred_code_id = code_logit.max(dim=2)[1]
                    input_code_id = pred_code_id
                else:
                    # Teacher forcing
                    input_code_id = label_dict['input_code_id']
                assert input_code_id.size() == (B, self.args.n_grids), input_code_id.size()

                input_code = self.vis_emb(input_code_id)
                assert input_code.size() == (B, self.args.n_grids, self.args.codebook_dim), input_code.size()

                input_code = input_code.permute(0,2,1).view(B, self.args.codebook_dim, self.args.grid_size, self.args.grid_size)
                fake_img = self.G(input_code)
                out_dict['fake_img'] = fake_img

            else:
                out_dict['code_logit'] = code_logit

                # [B, n_grids]
                pred_code_id = code_logit.max(dim=2)[1]
                out_dict['pred_code_id'] = pred_code_id

                # [B, n_grids, codebook_dim]
                pred_code = self.vis_emb(pred_code_id)
                out_dict['pred_code'] = pred_code

                pred_code = pred_code.permute(0,2,1).view(B, self.args.codebook_dim, self.args.grid_size, self.args.grid_size)
                fake_img = self.G(pred_code)
                out_dict['fake_img'] = fake_img

                out_dict['feat'] = regressed_code

        if task == 'word_mask':
            if calc_loss:
                word_labels = label_dict['word_labels']
                lm_loss = self.cross_entropy(
                    lang_prediction_scores.view(-1, self.config.vocab_size),
                    word_labels.view(-1)
                )
                out_dict['lm_loss'] = lm_loss
            else:
                out_dict['word_logit'] = lang_prediction_scores

        if task == 'matched':
            assert calc_loss is True
            matched_labels = label_dict['matched_labels']
            matched_loss = self.cross_entropy(
                cross_relationship_score.view(-1, 2),
                matched_labels.view(-1)
            )
            out_dict['matched_loss'] = matched_loss

        return out_dict
