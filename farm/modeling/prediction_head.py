import json
import logging
import os
import numpy as np

import torch
from dotmap import DotMap
from pytorch_pretrained_bert.modeling import BertLMPredictionHead
from torch import nn
from torch.nn import CrossEntropyLoss

from farm.data_handler.utils import is_json
from farm.utils import convert_iob_to_simple_tags

logger = logging.getLogger(__name__)


class PredictionHead(nn.Module):
    """ Takes word embeddings from a language model and generates logits for a given task. Can also convert logits
    to loss and and logits to predictions. """

    subclasses = {}

    def __init_subclass__(cls, **kwargs):
        """ This automatically keeps track of all available subclasses.
        Enables generic load() and load_from_dir() for all specific PredictionHead implementation.
        """
        super().__init_subclass__(**kwargs)
        cls.subclasses[cls.__name__] = cls

    @classmethod
    def create(cls, prediction_head_name, layer_dims, class_weights=None):
        """
        Create subclass of Prediction Head
        :param prediction_head_name: Classname (exact string!) of prediction head we want to create
        :type prediction_head_name: str
        :param layer_dims: describing the feed forward block structure, e.g. [768,2]
        :type layer_dims: List[Int]
        :param class_weights: weighting (
        :paramimbalanced) classes
        :type class_weights: list[Float]
        :return: Prediction Head of type prediction_head_name
        :rtype: PredictionHead[T]
        """
        # TODO make we want to make this more generic.
        #  1. Class weights is not relevant for all heads.
        #  2. Layer weights impose FF structure, maybe we want sth else later
        # Solution: We could again use **kwargs
        return cls.subclasses[prediction_head_name](
            layer_dims=layer_dims, class_weights=class_weights
        )

    def save_config(self, save_dir, head_num=0):
        """
        Saves config
        :param save_dir: path to save config to
        :type save_dir: str
        :param head_num: which head to save
        :type head_num: int
        :return: into the void
        """
        output_config_file = os.path.join(
            save_dir, f"prediction_head_{head_num}_config.json"
        )
        with open(output_config_file, "w") as file:
            json.dump(self.config, file)

    def save(self, save_dir, head_num=0):
        """
        Saves the prediction head
        :param save_dir: path to save prediction head to
        :type save_dir: str
        :param head_num: which head to save
        :type head_num: int
        :return: into the void
        """
        output_model_file = os.path.join(save_dir, f"prediction_head_{head_num}.bin")
        torch.save(self.state_dict(), output_model_file)
        self.save_config(save_dir, head_num)

    def generate_config(self):
        """
        Generates config file from Class parameters (only for sensible config parameters)
        :return: into the void
        """
        config = {}
        for key, value in self.__dict__.items():
            if is_json(value) and key[0] != "_":
                config[key] = value
        config["name"] = self.__class__.__name__
        self.config = config

    @classmethod
    def load(cls, model_file, config_file, device):
        """
        Loads a Prediction Head
        :param model_file: location where model is stored
        :type model_file: str
        :param config_file: location where corresponding config is stored
        :type config_file: str
        :param device: to which device we want to sent the model, either cpu or cuda
        :type device: torch.device
        :return: PredictionHead
        :rtype: PredictionHead[T]
        """
        config = json.load(open(config_file))
        prediction_head = cls.subclasses[config["name"]](**config)
        logger.info("Loading prediction head from {}".format(model_file))
        prediction_head.load_state_dict(torch.load(model_file, map_location=device))
        return prediction_head

    def logits_to_loss(self, logits, labels):
        """
        Implement this function in your special Prediction Head.
        Should combine logits and labels with a loss fct to a per sample loss
        :param logits: logits, can vary in shape and type, depending on task
        :type logits: object
        :param labels: labels, can vary in shape and type, depending on task
        :type labels: object
        :return: per sample loss
        :rtype: torch.tensor shape: [batch_size]
        """
        raise NotImplementedError()

    def logits_to_preds(self, logits):
        """
        Implement this function in your special Prediction Head.
        Should combine turn logits into predictions.
        :param logits: logits, can vary in shape and type, depending on task
        :type logits: object
        :return: predictions
        :rtype: torch.tensor shape: [batch_size]
        """
        raise NotImplementedError()

    def prepare_labels(self, **kwargs):
        """
        Some prediction heads need additional label conversion.
        E.g. NER needs word level labels turned into subword token level labels...
        :param kwargs: placeholder for passing generic parameters
        :type kwargs: object
        :return: labels in the right format
        :rtype: object
        """
        # TODO maybe just return **kwargs to not force people to implement this
        raise NotImplementedError()


class TextClassificationHead(PredictionHead):
    def __init__(
        self,
        layer_dims,
        class_weights=None,
        loss_ignore_index=-100,
        loss_reduction="none",
        **kwargs,
    ):
        super(TextClassificationHead, self).__init__()
        # num_labels could in most cases also be automatically retrieved from the data processor
        self.layer_dims = layer_dims
        # TODO is this still needed?
        self.feed_forward = FeedForwardBlock(self.layer_dims)
        self.num_labels = self.layer_dims[-1]
        self.ph_output_type = "per_sequence"
        self.model_type = "text_classification"
        self.class_weights = class_weights

        if class_weights:
            self.balanced_weights = nn.Parameter(
                torch.tensor(class_weights), requires_grad=False
            )
            self.loss_fct = CrossEntropyLoss(
                weight=self.balanced_weights,
                reduction=loss_reduction,
                ignore_index=loss_ignore_index,
            )
        else:
            self.loss_fct = CrossEntropyLoss(
                reduction=loss_reduction, ignore_index=loss_ignore_index
            )
        self.generate_config()

    def forward(self, X):
        logits = self.feed_forward(X)
        return logits

    def logits_to_loss(self, logits, label_ids, **kwargs):
        return self.loss_fct(logits, label_ids.view(-1))

    def logits_to_probs(self, logits, **kwargs):
        softmax = torch.nn.Softmax(dim=1)
        probs = softmax(logits)
        probs = torch.max(probs, dim=1)[0]
        probs = probs.cpu().numpy()
        return probs

    def logits_to_preds(self, logits, label_map, **kwargs):
        logits = logits.cpu().numpy()
        pred_ids = logits.argmax(1)
        preds = [label_map[x] for x in pred_ids]
        return preds

    def prepare_labels(self, label_map, label_ids, **kwargs):
        label_ids = label_ids.cpu().numpy()
        labels = [label_map[int(x)] for x in label_ids]
        return labels

    def formatted_preds(self, logits, label_map, samples, **kwargs):
        preds = self.logits_to_preds(logits, label_map)
        probs = self.logits_to_probs(logits)
        contexts = [sample.clear_text["text"] for sample in samples]

        assert len(preds) == len(probs) == len(contexts)

        res = {"task": "text_classification", "predictions": []}
        for pred, prob, context in zip(preds, probs, contexts):
            res["predictions"].append(
                {
                    "start": None,
                    "end": None,
                    "context": f"{context}",
                    "label": f"{pred}",
                    "probability": prob,
                }
            )
        return res


class TokenClassificationHead(PredictionHead):
    def __init__(self, layer_dims, **kwargs):
        super(TokenClassificationHead, self).__init__()

        self.layer_dims = layer_dims
        self.feed_forward = FeedForwardBlock(self.layer_dims)
        self.num_labels = self.layer_dims[-1]
        self.loss_fct = CrossEntropyLoss(reduction="none")
        self.ph_output_type = "per_token"
        self.model_type = "token_classification"
        self.generate_config()

    def forward(self, X):
        logits = self.feed_forward(X)
        return logits

    def logits_to_loss(
        self, logits, label_ids, initial_mask, padding_mask=None, **kwargs
    ):
        # Todo: should we be applying initial mask here? Loss is currently calculated even on non initial tokens
        active_loss = padding_mask.view(-1) == 1
        active_logits = logits.view(-1, self.num_labels)[active_loss]
        active_labels = label_ids.view(-1)[active_loss]
        loss = self.loss_fct(
            active_logits, active_labels
        )  # loss is a 1 dimemnsional (active) token loss
        return loss

    def logits_to_preds(self, logits, initial_mask, label_map, **kwargs):
        preds_word_all = []
        preds_tokens = torch.argmax(logits, dim=2)
        preds_token = preds_tokens.detach().cpu().numpy()
        # used to be: padding_mask = padding_mask.detach().cpu().numpy()
        initial_mask = initial_mask.detach().cpu().numpy()

        for idx, im in enumerate(initial_mask):
            preds_t = preds_token[idx]
            # Get labels and predictions for just the word initial tokens
            preds_word_id = self.initial_token_only(preds_t, initial_mask=im)
            preds_word = [label_map[pwi] for pwi in preds_word_id]
            preds_word_all.append(preds_word)
        return preds_word_all

    def logits_to_probs(self, logits, initial_mask, **kwargs):
        # get per token probs
        softmax = torch.nn.Softmax(dim=2)
        token_probs = softmax(logits)
        token_probs = torch.max(token_probs, dim=2)[0]
        token_probs = token_probs.cpu().numpy()

        # convert to per word probs
        all_probs = []
        initial_mask = initial_mask.detach().cpu().numpy()
        for idx, im in enumerate(initial_mask):
            probs_t = token_probs[idx]
            probs_words = self.initial_token_only(probs_t, initial_mask=im)
            all_probs.append(probs_words)
        return all_probs

    def prepare_labels(self, label_map, label_ids, initial_mask, **kwargs):
        labels_all = []
        label_ids = label_ids.cpu().numpy()
        for label_ids_one_sample, initial_mask_one_sample in zip(
            label_ids, initial_mask
        ):
            label_ids = self.initial_token_only(
                label_ids_one_sample, initial_mask_one_sample
            )
            labels = [label_map[l] for l in label_ids]
            labels_all.append(labels)
        return labels_all

    @staticmethod
    def initial_token_only(seq, initial_mask):
        ret = []
        for init, s in zip(initial_mask, seq):
            if init:
                ret.append(s)
        return ret

    def formatted_preds(self, logits, label_map, initial_mask, samples, **kwargs):
        preds = self.logits_to_preds(logits, initial_mask, label_map)
        probs = self.logits_to_probs(logits, initial_mask)

        # align back with original input by getting the original word spans
        spans = []
        for sample, sample_preds in zip(samples, preds):
            word_spans = []
            span = None
            for token, offset, start_of_word in zip(
                sample.tokenized["tokens"],
                sample.tokenized["offsets"],
                sample.tokenized["start_of_word"],
            ):
                if start_of_word:
                    # previous word has ended unless it's the very first word
                    if span is not None:
                        word_spans.append(span)
                    span = {"start": offset, "end": offset + len(token)}
                else:
                    # expand the span to include the subword-token
                    span["end"] = offset + len(token.replace("##", ""))
            word_spans.append(span)
            spans.append(word_spans)

        assert len(preds) == len(probs) == len(spans)

        res = {"task": "ner", "predictions": []}
        for preds_seq, probs_seq, sample, spans_seq in zip(
            preds, probs, samples, spans
        ):
            tags, spans_seq = convert_iob_to_simple_tags(preds_seq, spans_seq)
            seq_res = []
            for tag, prob, span in zip(tags, probs_seq, spans_seq):
                context = sample.clear_text["text"][span["start"] : span["end"]]
                seq_res.append(
                    {
                        "start": span["start"],
                        "end": span["end"],
                        "context": f"{context}",
                        "label": f"{tag}",
                        "probability": prob,
                    }
                )
            res["predictions"].extend(seq_res)
        return res


class BertLMHead(PredictionHead):
    def __init__(self, embeddings, hidden_size, hidden_act="gelu", **kwargs):
        super(BertLMHead, self).__init__()

        config = {"hidden_size": hidden_size, "hidden_act": hidden_act}
        config = DotMap(config, _dynamic=False)
        embeddings_weights = embeddings.word_embeddings.weight

        self.model = BertLMPredictionHead(config, embeddings_weights)
        self.loss_fct = CrossEntropyLoss(reduction="none", ignore_index=-1)
        self.num_labels = embeddings_weights.shape[0]  # vocab size
        # TODO Check if weight init needed!
        # self.apply(self.init_bert_weights)
        self.ph_output_type = "per_token"
        # TODO add model type for loading self.model_type = "language_modelling"
        self.generate_config()

    def save(self, save_dir, head_num=0):
        logger.warning("The weights of BertLMHead are not saved")
        self.save_config(save_dir, head_num)

    @classmethod
    def load(cls, model_file, config_file):
        raise NotImplementedError("BertLMHead does not currently support loading")

    def forward(self, X):
        lm_logits = self.model(X)
        return lm_logits

    def logits_to_loss(self, logits, lm_label_ids, **kwargs):
        batch_size = lm_label_ids.shape[0]
        masked_lm_loss = self.loss_fct(
            logits.view(-1, self.num_labels), lm_label_ids.view(-1)
        )
        per_sample_loss = masked_lm_loss.view(-1, batch_size).mean(dim=0)
        return per_sample_loss

    def logits_to_preds(self, logits, label_map, lm_label_ids, **kwargs):
        logits = logits.cpu().numpy()
        lm_label_ids = lm_label_ids.cpu().numpy()
        lm_preds_ids = logits.argmax(2)
        # apply mask to get rid of predictions for non-masked tokens
        assert lm_preds_ids.shape == lm_label_ids.shape
        lm_preds_ids[lm_label_ids == -1] = -1
        lm_preds_ids = lm_preds_ids.tolist()
        preds = []
        # we have a batch of sequences here. we need to convert for each token in each sequence.
        for pred_ids_for_sequence in lm_preds_ids:
            preds.append(
                [label_map[int(x)] for x in pred_ids_for_sequence if int(x) != -1]
            )
        return preds

    def prepare_labels(self, label_map, lm_label_ids, **kwargs):
        label_ids = lm_label_ids.cpu().numpy().tolist()
        labels = []
        # we have a batch of sequences here. we need to convert for each token in each sequence.
        for ids_for_sequence in label_ids:
            labels.append([label_map[int(x)] for x in ids_for_sequence if int(x) != -1])
        return labels


class FeedForwardBlock(nn.Module):
    """ A feed forward neural network of variable depth and width. """

    def __init__(self, layer_dims, **kwargs):
        # Todo: Consider having just one input argument
        super(FeedForwardBlock, self).__init__()

        # If read from config the input will be string
        n_layers = len(layer_dims) - 1
        layers_all = []
        # TODO: IS this needed?
        self.output_size = layer_dims[-1]

        for i in range(n_layers):
            size_in = layer_dims[i]
            size_out = layer_dims[i + 1]
            layer = nn.Linear(size_in, size_out)
            layers_all.append(layer)
        self.feed_forward = nn.Sequential(*layers_all)

    def forward(self, X):
        logits = self.feed_forward(X)
        return logits


class QuestionAnsweringHead(PredictionHead):
    """
    A question answering head predicts the start and end of the answer on token level.
    """

    def __init__(self, layer_dims, **kwargs):
        """
        :param layer_dims: dimensions of Feed Forward block, e.g. [768,2], for adjusting to BERT embedding. Output should be always 2
        :type layer_dims: List[Int]
        :param kwargs: placeholder for passing generic parameters
        :type kwargs: object
        """
        super(QuestionAnsweringHead, self).__init__()
        self.layer_dims = layer_dims
        self.feed_forward = FeedForwardBlock(self.layer_dims)
        self.num_labels = self.layer_dims[-1]
        self.ph_output_type = "per_token_squad"
        self.model_type = (
            "span_classification"
        )  # predicts start and end token of answer
        self.generate_config()

    def forward(self, X):
        """
        One forward pass through the prediction head model, starting with language model output on token level
        :param X: Output of language model, of shape [batch_size, seq_length, LM_embedding_dim]
        :type X: torch.tensor
        :return: (start_logits, end_logits), logits for the start and end of answer
        :rtype: tuple[torch.tensor,torch.tensor]
        """
        logits = self.feed_forward(X)
        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)
        return (start_logits, end_logits)

    def logits_to_loss(self, logits, start_position, end_position, **kwargs):
        """
        Combine predictions and labels to a per sample loss.
        :param logits: (start_logits, end_logits), logits for the start and end of answer
        :type logits: tuple[torch.tensor,torch.tensor]
        :param start_position: tensor with indices of START positions per sample
        :type start_position: torch.tensor
        :param end_position: tensor with indices of END positions per sample
        :type end_position: torch.tensor
        :param kwargs: placeholder for passing generic parameters
        :type kwargs: object
        :return: per_sample_loss: Per sample loss : )
        :rtype: torch.tensor
        """
        (start_logits, end_logits) = logits

        if len(start_position.size()) > 1:
            start_position = start_position.squeeze(-1)
        if len(end_position.size()) > 1:
            end_position = end_position.squeeze(-1)
        # sometimes the start/end positions (the labels read from file) are outside our model predictions, we ignore these terms
        ignored_index = start_logits.size(1)
        start_position.clamp_(0, ignored_index)
        end_position.clamp_(0, ignored_index)

        loss_fct = CrossEntropyLoss(ignore_index=ignored_index, reduction="none")
        start_loss = loss_fct(start_logits, start_position)
        end_loss = loss_fct(end_logits, end_position)
        per_sample_loss = (start_loss + end_loss) / 2
        return per_sample_loss

    def logits_to_preds(self, logits, **kwargs):
        """
        Get the predicted index of start and end token of the answer.
        :param logits: (start_logits, end_logits), logits for the start and end of answer
        :type logits: tuple[torch.tensor,torch.tensor]
        :param kwargs: placeholder for passing generic parameters
        :type kwargs: object
        :return: (start_idx, end_idx), start and end indices for all samples in batch
        :rtype: (torch.tensor,torch.tensor)
        """
        (start_logits, end_logits) = logits
        # TODO add checking for validity, e.g. end_idx coming after start_idx
        start_idx = torch.argmax(start_logits, dim=1)
        end_idx = torch.argmax(end_logits, dim=1)
        return (start_idx, end_idx)

    def prepare_labels(self, start_position, end_position, **kwargs):
        """
        We want to pack labels into a tuple, to be compliant with later functions
        :param start_position: indices of answer start positions (in token space)
        :type start_position: torch.tensor
        :param end_position: indices of answer end positions (in token space)
        :type end_position: torch.tensor
        :param kwargs: placeholder for passing generic parameters
        :type kwargs: object
        :return: tuplefied positions
        :rtype: tuple(torch.tensor,torch.tensor)
        """
        return (start_position, end_position)

    def formatted_preds(self, logits, samples, segment_ids, **kwargs) -> [str]:
        """
        Format predictions into actual answer strings (substrings of context). Used for Inference!
        :param logits: (start_logits, end_logits), logits for the start and end of answer
        :type logits: tuple[torch.tensor,torch.tensor]
        :param samples: converted samples, to get a hook onto the actual text
        :type samples: FARM.data_handler.samples.Sample
        :param segment_ids: used to separate question and context tokens
        :type segment_ids: torch.tensor
        :param kwargs: placeholder for passing generic parameters
        :type kwargs: object
        :return: Answers to the (ultimate) questions
        :rtype: list(str)
        """
        all_preds = []
        # TODO fix inference bug, model.forward is somehow packing logits into list
        # logits = logits[0]
        (start_idx, end_idx) = self.logits_to_preds(logits=logits)
        # we have char offsets for the questions context in samples.tokenized
        # we have start and end idx, but with the question tokens in front
        # lets shift this by the index of first segment ID corresponding to context
        start_idx = start_idx.cpu().numpy()
        end_idx = end_idx.cpu().numpy()
        segment_ids = segment_ids.cpu().numpy()

        shifts = np.argmax(segment_ids > 0, axis=1)
        start_idx = start_idx - shifts
        start_idx[start_idx < 0] = 0
        end_idx = end_idx - shifts
        end_idx[end_idx < 0] = 0
        end_idx = end_idx + 1  # slicing up to and including end
        result = {}
        result["task"] = "qa"

        # TODO features and samples might not be aligned. We still sometimes split a sample into multiple features
        for i, sample in enumerate(samples):
            answer = " ".join(sample.tokenized["tokens"][start_idx[i]: end_idx[i]])
            answer = answer.replace(" ##", "")
            answer = answer.replace("##", "")

            question = sample.clear_text["question_text"]
            pred = {}
            pred["start"] = sample.tokenized["offsets"][start_idx[i]]
            pred["end"] = sample.tokenized["offsets"][end_idx[i]]
            pred["context"] = question
            pred["label"] = answer
            pred["probability"] = "unkown" # TODO add prob from logits. Dunno how though
            answer_dugging = " ".join(sample.clear_text["doc_tokens"])[pred["start"]:pred["end"]]
            all_preds.append(pred)

        result["predictions"] = all_preds
        return result
